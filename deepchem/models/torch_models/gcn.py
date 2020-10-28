"""
DGL-based GCN for graph property prediction.
"""
import torch.nn as nn
import torch.nn.functional as F

from deepchem.models.losses import L2Loss, SparseSoftmaxCrossEntropy
from deepchem.models.torch_models.torch_model import TorchModel

class GCN(nn.Module):
    """Model for Graph Property Prediction Based on Graph Convolution Networks (GCN).

    This model proceeds as follows:

    * Update node representations in graphs with a variant of GCN
    * For each graph, compute its representation by 1) a weighted sum of the node
      representations in the graph, where the weights are computed by applying a
      gating function to the node representations 2) a max pooling of the node
      representations 3) concatenating the output of 1) and 2)
    * Perform the final prediction using an MLP

    Examples
    --------

    >>> import deepchem as dc
    >>> import pymatgen as mg
    >>> from deepchem.models import GCN
    >>> lattice = mg.Lattice.cubic(4.2)
    >>> structure = mg.Structure(lattice, ["Cs", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
    >>> featurizer = dc.feat.CGCNNFeaturizer()
    >>> cgcnn_graph = featurizer.featurize([structure])[0]
    >>> cgcnn_graph.num_node_features
    92
    >>> cgcnn_dgl_graph = cgcnn_graph.to_dgl_graph()
    >>> model = GCN(in_node_dim=92, hidden_node_dim=92, num_gnn_layers=2)
    >>> # Call model.eval as batch norm is implemented
    >>> model.eval()
    >>> model(cgcnn_dgl_graph)

    References
    ----------
    .. [1] Thomas N. Kipf and Max Welling. "Semi-Supervised Classification with Graph
       Convolutional Networks." ICLR 2017.

    Notes
    -----
    This class requires DGL (https://github.com/dmlc/dgl) and DGL-LifeSci
    (https://github.com/awslabs/dgl-lifesci) to be installed.

    This model is different from deepchem.models.GraphConvModel as follows:

    * For each graph convolution, the learnable weight in this model is shared across all nodes.
      ``GraphConvModel`` employs separate learnable weights for nodes of different degrees. A
      learnable weight is shared across all nodes of a particular degree.
    * For ``GraphConvModel``, there is an additional GraphPool operation after each
      graph convolution. The operation updates the representation of a node by applying an
      element-wise maximum over the representations of its neighbors and itself.
    * For computing graph-level representations, this model computes a weighted sum and an
      element-wise maximum of the representations of all nodes in a graph and concatenates them.
      The node weights are obtained by using a linear/dense layer followd by a sigmoid function.
      For ``GraphConvModel``, the sum over node representations is unweighted.
    * There are various minor differences in using dropout, skip connection and batch
      normalization.
    """
    def __init__(self,
                 n_tasks: int,
                 graph_conv_layers: list = None,
                 activation = None,
                 residual: bool = True,
                 batchnorm: bool = False,
                 dropout: float = 0.,
                 predictor_hidden_feats: int = 128,
                 predictor_dropout: float = 0.,
                 mode: str = 'regression',
                 number_atom_features: int = 75,
                 n_classes: int = 2,
                 nfeat_name: str = 'x'):
        """
        Parameters
        ----------
        n_tasks: int
            Number of tasks.
        graph_conv_layers: list of int
            Width of channels for GCN layers. graph_conv_layers[i] gives the width of channel
            for the i-th GCN layer. If not specified, the default value will be [64, 64].
        activation: callable
            The activation function to apply to the output of each GCN layer.
            By default, no activation function will be applied.
        residual: bool
            Whether to add a residual connection within each GCN layer. Default to True.
        batchnorm: bool
            Whether to apply batch normalization to the output of each GCN layer.
            Default to False.
        dropout: float
            The dropout probability for the output of each GCN layer. Default to 0.
        predictor_hidden_feats: int
            The size for hidden representations in the output MLP predictor. Default to 128.
        predictor_dropout: float
            The dropout probability in the output MLP predictor. Default to 0.
        mode: str
            The model type, 'classification' or 'regression'.
        number_atom_features: int
            The length of the initial atom feature vectors. Default to 75.
        n_classes: int
            The number of classes to predict per task
            (only used when ``mode`` is 'classification').
        nfeat_name: str
            For an input graph ``g``, the model assumes that it stores node features in
            ``g.ndata[nfeat_name]`` and will retrieve input node features from that.
        """
        try:
            import dgl
        except:
            raise ImportError('This class requires dgl.')
        try:
            import dgllife
        except:
            raise ImportError('This class requires dgllife.')

        if mode not in ['classification', 'regression']:
            raise ValueError("mode must be either 'classification' or 'regression'")

        super(GCN, self).__init__()

        self.n_tasks = n_tasks
        self.mode = mode
        self.n_classes = n_classes
        self.nfeat_name = nfeat_name
        if mode == 'classification':
            out_size = n_tasks * n_classes
        else:
            out_size = n_tasks

        from dgllife.model import GCNPredictor as DGLGCNPredictor

        if graph_conv_layers is None:
            graph_conv_layers = [64, 64]
        num_gnn_layers = len(graph_conv_layers)

        if activation is not None:
            activation = [activation] * num_gnn_layers

        self.model = DGLGCNPredictor(in_feats=number_atom_features,
                                     hidden_feats=graph_conv_layers,
                                     activation=activation,
                                     residual=[residual] * num_gnn_layers,
                                     batchnorm=[batchnorm] * num_gnn_layers,
                                     dropout=[dropout] * num_gnn_layers,
                                     n_tasks=out_size,
                                     predictor_hidden_feats=predictor_hidden_feats,
                                     predictor_dropout=predictor_dropout)

    def forward(self, g):
        """Predict graph labels

        Parameters
        ----------
        g: DGLGraph
            A DGLGraph for a batch of graphs. It stores the node features in
            ``dgl_graph.ndata[self.nfeat_name]``.

        Returns
        -------
        torch.Tensor
            The model output.

            * When self.mode = 'regression',
              its shape will be ``(dgl_graph.batch_size, self.n_tasks)``.
            * When self.mode = 'classification', the output consists of probabilities
              for classes. Its shape will be
              ``(dgl_graph.batch_size, self.n_tasks, self.n_classes)`` if self.n_tasks > 1;
              its shape will be ``(dgl_graph.batch_size, self.n_classes)`` if self.n_tasks is 1.
        torch.Tensor, optional
            This is only returned when self.mode = 'classification', the output consists of the
            logits for classes before softmax.
        """
        node_feats = g.ndata.pop(self.nfeat_name)
        out = self.model(g, node_feats)

        if self.mode == 'classification':
            if self.n_tasks == 1:
                logits = out.view(-1, self.n_classes)
                softmax_dim = 1
            else:
                logits = out.view(-1, self.n_tasks, self.n_classes)
                softmax_dim = 2
            proba = F.softmax(logits, dim=softmax_dim)
            return proba, logits
        else:
            return out

class GCNModel(TorchModel):
    """Model for Graph Property Prediction Based on Graph Convolution Networks (GCN).

    This model proceeds as follows:

    * Update node representations in graphs with a variant of GCN
    * For each graph, compute its representation by 1) a weighted sum of the node
      representations in the graph, where the weights are computed by applying a
      gating function to the node representations 2) a max pooling of the node
      representations 3) concatenating the output of 1) and 2)
    * Perform the final prediction using an MLP

    Examples
    --------

    >>> import deepchem as dc
    >>> from deepchem.models import GCNModel
    >>> dataset_config = {"reload": False, "featurizer": dc.feat.CGCNNFeaturizer, "transformers": []}
    >>> tasks, datasets, transformers = dc.molnet.load_perovskite(**dataset_config)
    >>> train, valid, test = datasets
    >>> model = dc.models.CGCNNModel(mode='regression', batch_size=32, learning_rate=0.001)
    >>> model.fit(train, nb_epoch=50)

    References
    ----------
    .. [1] Thomas N. Kipf and Max Welling. "Semi-Supervised Classification with Graph
       Convolutional Networks." ICLR 2017.

    Notes
    -----
    This class requires DGL (https://github.com/dmlc/dgl) and DGL-LifeSci
    (https://github.com/awslabs/dgl-lifesci) to be installed.

    This model is different from deepchem.models.GraphConvModel as follows:

    * For each graph convolution, the learnable weight in this model is shared across all nodes.
      ``GraphConvModel`` employs separate learnable weights for nodes of different degrees. A
      learnable weight is shared across all nodes of a particular degree.
    * For ``GraphConvModel``, there is an additional GraphPool operation after each
      graph convolution. The operation updates the representation of a node by applying an
      element-wise maximum over the representations of its neighbors and itself.
    * For computing graph-level representations, this model computes a weighted sum and an
      element-wise maximum of the representations of all nodes in a graph and concatenates them.
      The node weights are obtained by using a linear/dense layer followd by a sigmoid function.
      For ``GraphConvModel``, the sum over node representations is unweighted.
    * There are various minor differences in using dropout, skip connection and batch
      normalization.
    """
    def __init__(self,
                 n_tasks: int,
                 graph_conv_layers: list = None,
                 activation = None,
                 residual: bool = True,
                 batchnorm: bool = False,
                 dropout: float = 0.,
                 predictor_hidden_feats: int = 128,
                 predictor_dropout: float = 0.,
                 mode: str = 'regression',
                 number_atom_features=75,
                 n_classes: int = 2,
                 nfeat_name: str = 'x',
                 **kwargs):
        """
        Parameters
        ----------
        n_tasks: int
            Number of tasks.
        graph_conv_layers: list of int
            Width of channels for GCN layers. graph_conv_layers[i] gives the width of channel
            for the i-th GCN layer. If not specified, the default value will be [64, 64].
        activation: callable
            The activation function to apply to the output of each GCN layer.
            By default, no activation function will be applied.
        residual: bool
            Whether to add a residual connection within each GCN layer. Default to True.
        batchnorm: bool
            Whether to apply batch normalization to the output of each GCN layer.
            Default to False.
        dropout: float
            The dropout probability for the output of each GCN layer. Default to 0.
        predictor_hidden_feats: int
            The size for hidden representations in the output MLP predictor. Default to 128.
        predictor_dropout: float
            The dropout probability in the output MLP predictor. Default to 0.
        mode: str
            The model type, 'classification' or 'regression'.
        number_atom_features: int
            The length of the initial atom feature vectors. Default to 75.
        n_classes: int
            The number of classes to predict per task
            (only used when ``mode`` is 'classification').
        nfeat_name: str
            For an input graph ``g``, the model assumes that it stores node features in
            ``g.ndata[nfeat_name]`` and will retrieve input node features from that.
        kwargs
            This can include any keyword argument of TorchModel.
        """
        model = GCN(graph_conv_layers=graph_conv_layers,
                    activation=activation,
                    residual=residual,
                    batchnorm=batchnorm,
                    dropout=dropout,
                    predictor_hidden_feats=predictor_hidden_feats,
                    predictor_dropout=predictor_dropout,
                    n_tasks=n_tasks,
                    mode=mode,
                    number_atom_features=number_atom_features,
                    n_classes=n_classes,
                    nfeat_name=nfeat_name)
        if mode == 'regression':
            loss = L2Loss()
            output_types = ['prediction']
        else:
            loss = SparseSoftmaxCrossEntropy()
            output_types = ['prediction', 'loss']
        super(GCNModel, self).__init__(
            model, loss=loss, output_types=output_types, **kwargs)

    def _prepare_batch(self, batch):
        """Create batch data for GCN.

        Parameters
        ----------
        batch: tuple
            The tuple is ``(inputs, labels, weights)``.

        Returns
        -------
        inputs: DGLGraph
            DGLGraph for a batch of graphs.
        labels: list of torch.Tensor or None
            The graph labels.
        weights: list of torch.Tensor or None
            The weights for each sample or sample/task pair converted to torch.Tensor.
        """
        try:
            import dgl
        except:
            raise ImportError('This class requires dgl.')

        inputs, labels, weights = batch
        dgl_graphs = [graph.to_dgl_graph() for graph in inputs[0]]
        inputs = dgl.batch(dgl_graphs).to(self.device)
        _, labels, weights = super(GCNModel, self)._prepare_batch(([], labels, weights))
        return inputs, labels, weights
