from types import MappingProxyType
from typing import Dict
import logging
import uuid

import tqdm
import cloudpickle
import numpy as np

from libertem.common.buffers import BufferWrapper, AuxBufferWrapper
from libertem.common import Shape, Slice
from libertem.utils.threading import set_num_threads
from libertem.io.dataset.base import TilingScheme, Negotiator, Partition, DataSet
from libertem.corrections import CorrectionSet
from libertem.common.backend import get_use_cuda, get_device_class


log = logging.getLogger(__name__)


class UDFMeta:
    """
    UDF metadata. Makes all relevant metadata accessible to the UDF. Can be different
    for each task/partition.

    .. versionchanged:: 0.4.0
        Added distinction of dataset_dtype and input_dtype

    .. versionchanged:: 0.6.0.dev0
        Information on compute backend added
    """

    def __init__(self, partition_shape: Shape, dataset_shape: Shape, roi: np.ndarray,
                 dataset_dtype: np.dtype, input_dtype: np.dtype, tiling_scheme: TilingScheme = None,
                 tiling_index: int = 0, corrections=None, device_class: str = None):
        self._partition_shape = partition_shape
        self._dataset_shape = dataset_shape
        self._dataset_dtype = dataset_dtype
        self._input_dtype = input_dtype
        self._tiling_scheme = tiling_scheme
        self._tiling_index = tiling_index
        if device_class is None:
            device_class = 'cpu'
        self._device_class = device_class
        if roi is not None:
            roi = roi.reshape(dataset_shape.nav)
        self._roi = roi
        self._slice = None
        if corrections is None:
            corrections = CorrectionSet()
        self._corrections = corrections

    @property
    def slice(self) -> Slice:
        """
        Slice : A :class:`~libertem.common.slice.Slice` instance that describes the location
                within the dataset with navigation dimension flattened and reduced to the ROI.
        """
        return self._slice

    @slice.setter
    def slice(self, new_slice: Slice):
        self._slice = new_slice

    @property
    def partition_shape(self) -> Shape:
        """
        Shape : The shape of the partition this UDF currently works on.
                If a ROI was applied, the shape will be modified accordingly.
        """
        return self._partition_shape

    @property
    def dataset_shape(self) -> Shape:
        """
        Shape : The original shape of the whole dataset, not influenced by the ROI
        """
        return self._dataset_shape

    @property
    def tiling_scheme(self) -> Shape:
        """
        TilingScheme : the tiling scheme that was negotiated
        """
        return self._tiling_scheme

    @property
    def roi(self) -> np.ndarray:
        """
        numpy.ndarray : Boolean array which limits the elements the UDF is working on.
                     Has a shape of :attr:`dataset_shape.nav`.
        """
        return self._roi

    @property
    def dataset_dtype(self) -> np.dtype:
        """
        numpy.dtype : Native dtype of the dataset
        """
        return self._dataset_dtype

    @property
    def input_dtype(self) -> np.dtype:
        """
        numpy.dtype : dtype of the data that will be passed to the UDF

        This is determined from the dataset's native dtype and
        :meth:`UDF.get_preferred_input_dtype` using :meth:`numpy.result_type`

        .. versionadded:: 0.4.0
        """
        return self._input_dtype

    @property
    def corrections(self) -> CorrectionSet:
        """
        CorrectionSet : correction data that is available, either from the dataset
        or specified by the user

        .. versionadded:: 0.6.0
        """
        return self._corrections

    @property
    def device_class(self) -> str:
        '''
        Which device class is used.

        The back-end library can be accessed as :attr:`libertem.udf.base.UDF.xp`.
        This additional string information is used since that way the back-end can be probed without
        importing them all and testing them against :attr:`libertem.udf.base.UDF.xp`.

        Current values are :code:`cpu` (default) or :code:`cuda`.

        .. versionadded:: 0.6.0
        '''
        return self._device_class


class UDFData:
    '''
    Container for result buffers, return value from running UDFs
    '''

    def __init__(self, data: Dict[str, BufferWrapper]):
        self._data = data
        self._views = {}

    def __repr__(self) -> str:
        return "<UDFData: %r>" % (
            self._data
        )

    def __getattr__(self, k: str):
        if k.startswith("_"):
            raise AttributeError("no such attribute: %s" % k)
        try:
            return self._get_view_or_data(k)
        except KeyError as e:
            raise AttributeError(str(e))

    def get(self, k, default=None):
        try:
            return self.__getattr__(k)
        except KeyError:
            return default

    def __setattr__(self, k, v):
        if not k.startswith("_"):
            raise AttributeError(
                "cannot re-assign attribute %s, did you mean `.%s[:] = ...`?" % (
                    k, k
                )
            )
        super().__setattr__(k, v)

    def _get_view_or_data(self, k):
        if k in self._views:
            return self._views[k]
        res = self._data[k]
        if hasattr(res, 'raw_data'):
            return res.raw_data
        return res

    def __getitem__(self, k):
        return self._data[k]

    def __contains__(self, k):
        return k in self._data

    def items(self):
        return self._data.items()

    def keys(self):
        return self._data.keys()

    def as_dict(self) -> Dict[str, BufferWrapper]:
        return dict(self.items())

    def get_proxy(self):
        return MappingProxyType({
            k: (self._views[k] if k in self._views else self._data[k].raw_data)
            for k, v in self._data.items()
        })

    def _get_buffers(self, filter_allocated: bool = False):
        for k, buf in self._data.items():
            if not hasattr(buf, 'has_data') or (buf.has_data() and filter_allocated):
                continue
            yield k, buf

    def allocate_for_part(self, partition: Shape, roi: np.ndarray, lib=None):
        """
        allocate all BufferWrapper instances in this namespace.
        for pre-allocated buffers (i.e. aux data), only set shape and roi
        """
        for k, buf in self._get_buffers():
            buf.set_shape_partition(partition, roi)
        for k, buf in self._get_buffers(filter_allocated=True):
            buf.allocate(lib=lib)

    def allocate_for_full(self, dataset, roi: np.ndarray):
        for k, buf in self._get_buffers():
            buf.set_shape_ds(dataset, roi)
        for k, buf in self._get_buffers(filter_allocated=True):
            buf.allocate()

    def set_view_for_dataset(self, dataset):
        for k, buf in self._get_buffers():
            self._views[k] = buf.get_view_for_dataset(dataset)

    def set_view_for_partition(self, partition: Shape):
        for k, buf in self._get_buffers():
            self._views[k] = buf.get_view_for_partition(partition)

    def set_view_for_tile(self, partition, tile):
        for k, buf in self._get_buffers():
            self._views[k] = buf.get_view_for_tile(partition, tile)

    def set_contiguous_view_for_tile(self, partition, tile):
        # .. versionadded:: 0.5.0
        for k, buf in self._get_buffers():
            self._views[k] = buf.get_contiguous_view_for_tile(partition, tile)

    def flush(self):
        # .. versionadded:: 0.5.0
        for k, buf in self._get_buffers():
            buf.flush()

    def export(self):
        # .. versionadded:: 0.6.0.dev0
        for k, buf in self._get_buffers():
            buf.export()

    def set_view_for_frame(self, partition, tile, frame_idx):
        for k, buf in self._get_buffers():
            self._views[k] = buf.get_view_for_frame(partition, tile, frame_idx)

    def new_for_partition(self, partition, roi: np.ndarray):
        for k, buf in self._get_buffers():
            self._data[k] = buf.new_for_partition(partition, roi)

    def clear_views(self):
        self._views = {}


class UDFFrameMixin:
    '''
    Implement :code:`process_frame` for per-frame processing.
    '''

    def process_frame(self, frame: np.ndarray):
        """
        Implement this method to process the data on a frame-by-frame manner.

        Data available in this method:

        - `self.params`    - the parameters of this UDF
        - `self.task_data` - task data created by `get_task_data`
        - `self.results`   - the result buffer instances
        - `self.meta`      - meta data about the current operation and data set

        Parameters
        ----------
        frame : numpy.ndarray or cupy.ndarray
            A single frame or signal element from the dataset.
            The shape is the same as `dataset.shape.sig`. In case of pixelated
            STEM / scanning diffraction data this is 2D, for spectra 1D etc.
        """
        raise NotImplementedError()


class UDFTileMixin:
    '''
    Implement :code:`process_tile` for per-tile processing.
    '''

    def process_tile(self, tile: np.ndarray):
        """
        Implement this method to process the data in a tiled manner.

        Data available in this method:

        - `self.params`    - the parameters of this UDF
        - `self.task_data` - task data created by `get_task_data`
        - `self.results`   - the result buffer instances
        - `self.meta`      - meta data about the current operation and data set

        Parameters
        ----------
        tile : numpy.ndarray or cupy.ndarray
            A small number N of frames or signal elements from the dataset.
            The shape is (N,) + `dataset.shape.sig`. In case of pixelated
            STEM / scanning diffraction data this is 3D, for spectra 2D etc.
        """
        raise NotImplementedError()


class UDFPartitionMixin:
    '''
    Implement :code:`process_partition` for per-partition processing.
    '''

    def process_partition(self, partition: np.ndarray):
        """
        Implement this method to process the data partitioned into large
        (100s of MiB) partitions.

        Data available in this method:

        - `self.params`    - the parameters of this UDF
        - `self.task_data` - task data created by `get_task_data`
        - `self.results`   - the result buffer instances
        - `self.meta`      - meta data about the current operation and data set

        Note
        ----
        Only use this method if you know what you are doing; especially if
        you are running a processing pipeline with multiple steps, or multiple
        processing pipelines at the same time, performance may be adversely
        impacted.

        Parameters
        ----------
        partition : numpy.ndarray or cupy.ndarray
            A large number N of frames or signal elements from the dataset.
            The shape is (N,) + `dataset.shape.sig`. In case of pixelated
            STEM / scanning diffraction data this is 3D, for spectra 2D etc.
        """
        raise NotImplementedError()


class UDFPreprocessMixin:
    '''
    Implement :code:`preprocess` to initialize the result buffers of a partition on the worker
    before the partition data is processed.

    .. versionadded:: 0.3.0
    '''

    def preprocess(self):
        """
        Implement this method to preprocess the result data for a partition.

        This can be useful to initialize arrays of
        :code:`dtype='object'` with the correct container types, for example.

        Data available in this method:

        - `self.params`    - the parameters of this UDF
        - `self.task_data` - task data created by `get_task_data`
        - `self.results`   - the result buffer instances
        """
        raise NotImplementedError()


class UDFPostprocessMixin:
    '''
    Implement :code:`postprocess` to modify the resulf buffers of a partition on the worker
    after the partition data has been completely processed, but before it is returned to the
    master node for the final merging step.
    '''

    def postprocess(self):
        """
        Implement this method to postprocess the result data for a partition.

        This can be useful in combination with process_tile() to implement
        a postprocessing step that requires the reduced results for whole frames.

        Data available in this method:

        - `self.params`    - the parameters of this UDF
        - `self.task_data` - task data created by `get_task_data`
        - `self.results`   - the result buffer instances
        """
        raise NotImplementedError()


class UDFBase:
    '''
    Base class for UDFs with helper functions.
    '''

    def allocate_for_part(self, partition, roi):
        for ns in [self.results]:
            ns.allocate_for_part(partition, roi, lib=self.xp)

    def allocate_for_full(self, dataset, roi):
        for ns in [self.params, self.results]:
            ns.allocate_for_full(dataset, roi)

    def set_views_for_dataset(self, dataset):
        for ns in [self.params]:
            ns.set_view_for_dataset(dataset)

    def set_views_for_partition(self, partition):
        for ns in [self.params, self.results]:
            ns.set_view_for_partition(partition)

    def set_views_for_tile(self, partition, tile):
        for ns in [self.params, self.results]:
            ns.set_view_for_tile(partition, tile)

    def set_contiguous_views_for_tile(self, partition, tile):
        # .. versionadded:: 0.5.0
        for ns in [self.params, self.results]:
            ns.set_contiguous_view_for_tile(partition, tile)

    def flush(self):
        # .. versionadded:: 0.5.0
        for ns in [self.params, self.results]:
            ns.flush()

    def set_views_for_frame(self, partition, tile, frame_idx):
        for ns in [self.params, self.results]:
            ns.set_view_for_frame(partition, tile, frame_idx)

    def clear_views(self):
        for ns in [self.params, self.results]:
            ns.clear_views()

    def init_task_data(self):
        self.task_data = UDFData(self.get_task_data())

    def init_result_buffers(self):
        self.results = UDFData(self.get_result_buffers())

    def export_results(self):
        # .. versionadded:: 0.6.0.dev0
        self.results.export()

    def set_meta(self, meta):
        self.meta = meta

    def set_slice(self, slice_):
        self.meta.slice = slice_

    def set_backend(self, backend):
        assert backend in self.get_backends()
        self._backend = backend

    @property
    def xp(self):
        '''
        Compute back-end library to use.

        Generally, use :code:`self.xp` instead of :code`np` to use NumPy or CuPy transparently

        .. versionadded:: 0.6.0
        '''
        # Implemented as property and not variable to avoid pickling issues
        # tests/udf/test_simple_udf.py::test_udf_pickle
        if self._backend == 'numpy' or self._backend == 'cuda':
            return np
        elif self._backend == 'cupy':
            # Re-importing should be fast, right?
            # Importing only here to avoid superfluous import
            import cupy
            # mocking for testing without actual CUDA device
            # import numpy as cupy
            return cupy
        else:
            raise ValueError(f"Backend name can be 'numpy', 'cuda' or 'cupy', got {self._backend}")

    def get_method(self):
        if hasattr(self, 'process_tile'):
            method = 'tile'
        elif hasattr(self, 'process_frame'):
            method = 'frame'
        elif hasattr(self, 'process_partition'):
            method = 'partition'
        else:
            raise TypeError("UDF should implement one of the `process_*` methods")
        return method


class UDF(UDFBase):
    """
    The main user-defined functions interface. You can implement your functionality
    by overriding methods on this class.
    """
    USE_NATIVE_DTYPE = np.bool
    TILE_SIZE_BEST_FIT = object()
    TILE_SIZE_MAX = np.float("inf")
    TILE_DEPTH_DEFAULT = object()
    TILE_DEPTH_MAX = np.float("inf")

    def __init__(self, **kwargs):
        """
        Create a new UDF instance. If you override `__init__`, please take care,
        as it is called multiple times during evaluation of a UDF. You can handle
        some pre-conditioning of parameters, but you also have to accept the results
        as input again.

        Arguments passed as `**kwargs` will be automatically available on `self.params`
        when running the UDF.

        Example
        -------

        >>> class MyUDF(UDF):
        ...     def __init__(self, param1, param2="def2", **kwargs):
        ...         param1 = int(param1)
        ...         if "param3" not in kwargs:
        ...             raise TypeError("missing argument param3")
        ...         super().__init__(param1=param1, param2=param2, **kwargs)

        Parameters
        ----------
        kwargs
            Input parameters. They are scattered to the worker processes and
            available as `self.params` from here on.

            Values can be `BufferWrapper` instances, which, when accessed via
            `self.params.the_key_here`, will automatically return a view corresponding
            to the current unit of data (frame, tile, partition).
        """
        self._backend = 'numpy'  # default so that self.xp can always be used
        self._kwargs = kwargs
        self.params = UDFData(kwargs)
        self.task_data = None
        self.results = None
        self._requires_custom_merge = None

    def copy(self):
        return self.__class__(**self._kwargs)

    def copy_for_partition(self, partition: np.ndarray, roi: np.ndarray):
        """
        create a copy of the UDF, specifically slicing aux data to the
        specified pratition and roi
        """
        new_instance = self.__class__(**self._kwargs)
        new_instance.params.new_for_partition(partition, roi)
        return new_instance

    def get_task_data(self):
        """
        Initialize per-task data.

        Per-task data can be mutable. Override this function
        to allocate temporary buffers, or to initialize
        system resources.

        If you want to distribute static data, use
        parameters instead.

        Data available in this method:

        - `self.params` - the input parameters of this UDF
        - `self.meta` - relevant metadata, see :class:`UDFMeta` documentation.

        Returns
        -------
        dict
            Flat dict with string keys. Keys should
            be valid python identifiers, which allows
            access via `self.task_data.the_key_here`.
        """
        return {}

    def get_result_buffers(self):
        """
        Return result buffer declaration.

        Values of the returned dict should be `BufferWrapper`
        instances, which, when accessed via `self.results.key`,
        will automatically return a view corresponding to the
        current unit of data (frame, tile, partition).

        The values also need to be serializable via pickle.

        Data available in this method:

        - `self.params` - the parameters of this UDF
        - `self.meta` - relevant metadata, see :class:`UDFMeta` documentation.
            Please note that partition metadata will not be set when this method is
            executed on the head node.

        Returns
        -------
        dict
            Flat dict with string keys. Keys should
            be valid python identifiers, which allows
            access via `self.results.the_key_here`.
        """
        raise NotImplementedError()

    @property
    def requires_custom_merge(self):
        """
        Determine if buffers with :code:`kind != 'nav'` are present where
        the default merge doesn't work

        .. versionadded:: 0.5.0
        """
        if self._requires_custom_merge is None:
            buffers = self.get_result_buffers()
            self._requires_custom_merge = any(buffer.kind != 'nav' for buffer in buffers.values())
        return self._requires_custom_merge

    def merge(self, dest: Dict[str, np.array], src: Dict[str, np.array]):
        """
        Merge a partial result `src` into the current global result `dest`.

        Data available in this method:

        - `self.params` - the parameters of this UDF

        Parameters
        ----------

        dest
            global results; dictionary mapping the buffer name (from `get_result_buffers`)
            to a numpy array

        src
            results for a partition; dictionary mapping the buffer name (from `get_result_buffers`)
            to a numpy array

        Note
        ----
        This function is running on the leader node, which means `self.results`
        and `self.task_data` are not available.
        """
        if self.requires_custom_merge:
            raise NotImplementedError(
                "Default merging only works for kind='nav' buffers. "
                "Please implement a suitable custom merge function."
            )
        for k in dest:
            check_cast(dest[k], src[k])
            dest[k][:] = src[k]

    def get_preferred_input_dtype(self):
        '''
        Override this method to specify the preferred input dtype of the UDF.

        The default is :code:`float32` since most numerical processing tasks
        perform best with this dtype, namely dot products.

        The back-end uses this preferred input dtype in combination with the
        dataset`s native dtype to determine the input dtype using
        :meth:`numpy.result_type`. That means :code:`float` data in a dataset
        switches the dtype to :code:`float` even if this method returns an
        :code:`int` dtype. :code:`int32` or wider input data would switch from
        :code:`float32` to :code:`float64`, and complex data in the dataset will
        switch the input dtype kind to :code:`complex`, following the NumPy
        casting rules.

        In case your UDF only works with specific input dtypes, it should throw
        an error or warning if incompatible dtypes are used, and/or implement a
        meaningful conversion in your UDF's :code:`process_<...>` routine.

        If you prefer to always use the dataset's native dtype instead of
        floats, you can override this method to return
        :attr:`UDF.USE_NATIVE_DTYPE`, which is currently identical to
        :code:`numpy.bool` and behaves as a neutral element in
        :func:`numpy.result_type`.

        .. versionadded:: 0.4.0
        '''
        return np.float32

    def get_tiling_preferences(self):
        """
        Configure tiling preferences. Return a dictinary with the
        following keys:

        - "depth": number of frames/frame parts to stack on top of each other
        - "total_size": total size of a tile in bytes
        """
        return {
            "depth": UDF.TILE_DEPTH_DEFAULT,
            "total_size": UDF.TILE_SIZE_MAX,
        }

    def get_backends(self):
        # TODO see interaction with C++ CUDA modules requires a different type than CuPy
        '''
        Signal which computation back-ends the UDF can use.

        :code:`numpy` is the default CPU-based computation.

        :code:`cuda` is CUDA-based computation without CuPy.

        :code:`cupy` is CUDA-based computation through CuPy.

        .. versionadded:: 0.6.0

        Returns
        -------

        backend : Iterable[str]
            An iterable containing possible values :code:`numpy` (default), :code:`'cuda'` and
            :code:`cupy`
        '''
        return ('numpy', )

    def cleanup(self):  # FIXME: name? implement cleanup as context manager somehow?
        pass

    def buffer(self, kind, extra_shape=(), dtype="float32", where=None):
        '''
        Use this method to create :class:`~ libertem.common.buffers.BufferWrapper` objects
        in :meth:`get_result_buffers`.
        '''
        return BufferWrapper(kind, extra_shape, dtype, where)

    @classmethod
    def aux_data(cls, data, kind, extra_shape=(), dtype="float32"):
        """
        Use this method to create auxiliary data. Auxiliary data should
        have a shape like `(dataset.shape.nav, extra_shape)` and on access,
        an appropriate view will be created. For example, if you access
        aux data in `process_frame`, you will get the auxiliary data for
        the current frame you are processing.

        Example
        -------

        We create a UDF to demonstrate the behavior:

        >>> class MyUDF(UDF):
        ...     def get_result_buffers(self):
        ...         # Result buffer for debug output
        ...         return {'aux_dump': self.buffer(kind='nav', dtype='object')}
        ...
        ...     def process_frame(self, frame):
        ...         # Extract value of aux data for demonstration
        ...         self.results.aux_dump[:] = str(self.params.aux_data[:])
        ...
        >>> # for each frame, provide three values from a sequential series:
        >>> aux1 = MyUDF.aux_data(
        ...     data=np.arange(np.prod(dataset.shape.nav) * 3, dtype=np.float32),
        ...     kind="nav", extra_shape=(3,), dtype="float32"
        ... )
        >>> udf = MyUDF(aux_data=aux1)
        >>> res = ctx.run_udf(dataset=dataset, udf=udf)

        process_frame for frame (0, 7) received a view of aux_data with values [21., 22., 23.]:

        >>> res['aux_dump'].data[0, 7]
        '[21. 22. 23.]'
        """
        buf = AuxBufferWrapper(kind, extra_shape, dtype)
        buf.set_buffer(data)
        return buf


def check_cast(fromvar, tovar):
    if not np.can_cast(fromvar.dtype, tovar.dtype, casting='safe'):
        # FIXME exception or warning?
        raise TypeError("Unsafe automatic casting from %s to %s" % (fromvar.dtype, tovar.dtype))


class Task(object):
    """
    A computation on a partition. Inherit from this class and implement ``__call__``
    for your specific computation.

    .. versionchanged:: 0.4.0
        Moved from libertem.job.base to libertem.udf.base as part of Job API deprecation
    """

    def __init__(self, partition, idx):
        self.partition = partition
        self.idx = idx

    def get_locations(self):
        return self.partition.get_locations()

    def get_resources(self):
        '''
        Specify the resources that a Task will use.

        The resources are designed to work with resource tags in Dask clusters:
        See https://distributed.dask.org/en/latest/resources.html

        The resources allow scheduling of CPU-only compute, CUDA-only compute,
        (CPU or CUDA) hybrid compute, and service tasks in such a way that all
        resources are used without oversubscription.

        Each CPU worker gets one CPU and one compute resource assigned. Each
        CUDA worker gets one CUDA and one compute resource assigned. A service
        worker doesn't get any resources assigned.

        A CPU-only task consumes one CPU and one compute resource, i.e. it will
        be scheduled only on CPU workers. A CUDA-only task consumes one CUDA and
        one compute resource, i.e. it will only be scheduled on CUDA workers. A
        hybrid task that can run on both CPU or CUDA only consumes a compute
        resource, i.e. it can be scheduled on CPU or CUDA workers. A service
        task doesn't request any resources and can therefore run on CPU, CUDA
        and service workers.

        Which device a hybrid task uses is only decided at runtime by the
        environment variables that are set on each worker process.

        That way, CPU-only and CUDA-only tasks can run in parallel without
        oversubscription, and hybrid tasks use whatever compute workers are free
        at the time.

        Compute tasks will not run on service workers, so that these can serve
        shorter service tasks with lower latency.

        At the moment, workers only get a single "item" of each resource
        assigned since we run one process per CPU. Requesting more of a resource
        than any of the workers has causes a :class:`RuntimeError`, including
        requesting a resource that is not available at all.

        For that reason one has to make sure that the workers are set up with
        the correct resources and matching environment.
        :meth:`libertem.utils.devices.detect`,
        :meth:`libertem.executor.dask.cluster_spec` and
        :meth:`libertem.executor.dask.DaskJobExecutor.make_local` can be used to
        set up a local cluster correctly.

        .. versionadded:: 0.6.0
        '''
        # default: run only on CPU and do computation
        # No CUDA support for the deprecated Job interface
        return {'CPU': 1, 'compute': 1}

    def __call__(self):
        raise NotImplementedError()


class UDFTask(Task):
    def __init__(self, partition: Partition, idx, udfs, roi, backends, corrections=None):
        super().__init__(partition=partition, idx=idx)
        self._roi = roi
        self._udfs = udfs
        self._backends = backends
        self._corrections = corrections

    def __call__(self):
        return UDFRunner(self._udfs).run_for_partition(
            self.partition, self._roi, self._corrections,
        )

    def get_resources(self):
        """
        Intersection of resources of all UDFs, throws if empty.

        See docstring of super class for details.
        """
        backends_for_udfs = [
            set(udf.get_backends())
            for udf in self._udfs
        ]
        backends = set(backends_for_udfs[0])  # intersection of backends
        for backend_set in backends_for_udfs:
            backends.intersection_update(backend_set)
        # Limit to externally specified backends
        if self._backends is not None:
            backends = set(self._backends).intersection(backends)
        if len(backends) == 0:
            raise ValueError(
                "There is no common supported UDF backend (have: %r, limited to %r)"
                % (backends_for_udfs, self._backends)
            )
        if 'numpy' in backends and ('cupy' in backends or 'cuda' in backends):
            # Can be run on both CPU and CUDA workers
            return {'compute': 1}
        elif 'numpy' in backends:
            # Only run on CPU workers
            return {'CPU': 1, 'compute': 1}
        elif ('cupy' in backends or 'cuda' in backends):
            # Only run on CUDA workers
            return {'CUDA': 1, 'compute': 1}
        else:
            raise ValueError(f"UDF backends are {backends}, supported are "
                "'numpy', 'cuda' and 'cupy'")


class UDFRunner:
    def __init__(self, udfs, debug=False):
        self._udfs = udfs
        self._debug = debug

    def _get_dtype(self, dtype):
        tmp_dtype = dtype
        for udf in self._udfs:
            tmp_dtype = np.result_type(
                udf.get_preferred_input_dtype(),
                tmp_dtype
            )
        return tmp_dtype

    def _init_udfs(self, numpy_udfs, cupy_udfs, partition, roi, corrections, device_class):
        dtype = self._get_dtype(partition.dtype)
        meta = UDFMeta(
            partition_shape=partition.slice.adjust_for_roi(roi).shape,
            dataset_shape=partition.meta.shape,
            roi=roi,
            dataset_dtype=partition.dtype,
            input_dtype=dtype,
            tiling_scheme=None,
            corrections=corrections,
            device_class=device_class,
        )
        for udf in numpy_udfs:
            if device_class == 'cuda':
                udf.set_backend('cuda')
            else:
                udf.set_backend('numpy')
        if device_class == 'cpu':
            assert not cupy_udfs
        for udf in cupy_udfs:
            udf.set_backend('cupy')
        udfs = numpy_udfs + cupy_udfs
        for udf in udfs:
            udf.set_meta(meta)
            udf.init_result_buffers()
            udf.allocate_for_part(partition, roi)
            udf.init_task_data()
            if hasattr(udf, 'preprocess'):
                udf.clear_views()
                udf.preprocess()
        neg = Negotiator()
        # FIXME take compute backend into consideration as well
        # Other boundary conditions when moving input data to device
        tiling_scheme = neg.get_scheme(
            udfs=udfs,
            partition=partition,
            read_dtype=dtype,
            roi=roi,
        )

        # FIXME: don't fully re-create?
        meta = UDFMeta(
            partition_shape=partition.slice.adjust_for_roi(roi).shape,
            dataset_shape=partition.meta.shape,
            roi=roi,
            dataset_dtype=partition.dtype,
            input_dtype=dtype,
            tiling_scheme=tiling_scheme,
            corrections=corrections,
            device_class=device_class,
        )
        for udf in udfs:
            udf.set_meta(meta)
        return (meta, tiling_scheme, dtype)

    def _run_udfs(self, numpy_udfs, cupy_udfs, partition, tiling_scheme, roi, dtype):
        def run_tile(udfs, tile, device_tile):
            for udf in udfs:
                method = udf.get_method()
                if method == 'tile':
                    udf.set_contiguous_views_for_tile(partition, tile)
                    udf.set_slice(tile.tile_slice)
                    udf.process_tile(device_tile)
                elif method == 'frame':
                    tile_slice = tile.tile_slice
                    for frame_idx, frame in enumerate(device_tile):
                        frame_slice = Slice(
                            origin=(tile_slice.origin[0] + frame_idx,) + tile_slice.origin[1:],
                            shape=Shape((1,) + tuple(tile_slice.shape)[1:],
                                        sig_dims=tile_slice.shape.sig.dims),
                        )
                        udf.set_slice(frame_slice)
                        udf.set_views_for_frame(partition, tile, frame_idx)
                        udf.process_frame(frame)
                elif method == 'partition':
                    udf.set_views_for_tile(partition, tile)
                    udf.set_slice(partition.slice)
                    udf.process_partition(device_tile)

        # FIXME pass information on target location (numpy or cupy)
        # to dataset so that is can already move it there.
        # In the future, it might even decode data on the device instead of CPU
        tiles = partition.get_tiles(
            tiling_scheme=tiling_scheme,
            roi=roi, dest_dtype=dtype,
        )

        if cupy_udfs:
            xp = cupy_udfs[0].xp

        for tile in tiles:
            run_tile(numpy_udfs, tile, tile)
            if cupy_udfs:
                # Work-around, should come from dataset later
                device_tile = xp.asanyarray(tile)
                run_tile(cupy_udfs, tile, device_tile)

    def _wrapup_udfs(self, numpy_udfs, cupy_udfs, partition):
        udfs = numpy_udfs + cupy_udfs
        for udf in udfs:
            udf.flush()
            if hasattr(udf, 'postprocess'):
                udf.clear_views()
                udf.postprocess()

            udf.cleanup()
            udf.clear_views()
            udf.export_results()

        if self._debug:
            try:
                cloudpickle.loads(cloudpickle.dumps(partition))
            except TypeError:
                raise TypeError("could not pickle partition")
            try:
                cloudpickle.loads(cloudpickle.dumps(
                    [u.results for u in udfs]
                ))
            except TypeError:
                raise TypeError("could not pickle results")

    def _udf_lists(self, device_class):
        numpy_udfs = []
        cupy_udfs = []
        if device_class == 'cuda':
            for udf in self._udfs:
                backends = udf.get_backends()
                if 'cuda' in backends:
                    numpy_udfs.append(udf)
                elif 'cupy' in backends:
                    cupy_udfs.append(udf)
                else:
                    raise ValueError(f"UDF backends are {backends}, supported on CUDA are "
                            "'cuda' and 'cupy'")
        elif device_class == 'cpu':
            assert all('numpy' in udf.get_backends() for udf in self._udfs)
            numpy_udfs = self._udfs
        else:
            raise ValueError(f"Unknown device class {device_class}, "
                "supported are 'cpu' and 'cuda'")
        return (numpy_udfs, cupy_udfs)

    def run_for_partition(self, partition: Partition, roi, corrections):
        with set_num_threads(1):
            try:
                previous_id = None
                device_class = get_device_class()
                # numpy_udfs and cupy_udfs contain references to the objects in
                # self._udfs
                numpy_udfs, cupy_udfs = self._udf_lists(device_class)
                # Will only be populated if actually on CUDA worker
                # and any UDF supports 'cupy' (and not 'cuda')
                if cupy_udfs:
                    # Avoid importing if not used
                    import cupy
                    device = get_use_cuda()
                    previous_id = cupy.cuda.Device().id
                    cupy.cuda.Device(device).use()
                (meta, tiling_scheme, dtype) = self._init_udfs(
                    numpy_udfs, cupy_udfs, partition, roi, corrections, device_class
                )
                # print("UDF TilingScheme: %r" % tiling_scheme.shape)
                partition.set_corrections(corrections)
                self._run_udfs(numpy_udfs, cupy_udfs, partition, tiling_scheme, roi, dtype)
                self._wrapup_udfs(numpy_udfs, cupy_udfs, partition)
            finally:
                if previous_id is not None:
                    cupy.cuda.Device(previous_id).use()
            # Make sure results are in the same order as the UDFs
            return tuple(udf.results for udf in self._udfs)

    def _debug_task_pickling(self, tasks):
        if self._debug:
            cloudpickle.loads(cloudpickle.dumps(tasks))

    def _check_preconditions(self, dataset: DataSet, roi):
        if roi is not None and np.product(roi.shape) != np.product(dataset.shape.nav):
            raise ValueError(
                "roi: incompatible shapes: %s (roi) vs %s (dataset)" % (
                    roi.shape, dataset.shape.nav
                )
            )

    def _prepare_run_for_dataset(self, dataset: DataSet, executor, roi, corrections, backends):
        self._check_preconditions(dataset, roi)
        meta = UDFMeta(
            partition_shape=None,
            dataset_shape=dataset.shape,
            roi=roi,
            dataset_dtype=dataset.dtype,
            input_dtype=self._get_dtype(dataset.dtype),
            corrections=corrections,
        )
        for udf in self._udfs:
            udf.set_meta(meta)
            udf.init_result_buffers()
            udf.allocate_for_full(dataset, roi)

            if hasattr(udf, 'preprocess'):
                udf.set_views_for_dataset(dataset)
                udf.preprocess()

        tasks = list(self._make_udf_tasks(dataset, roi, corrections, backends))
        return tasks

    def run_for_dataset(self, dataset: DataSet, executor,
                        roi=None, progress=False, corrections=None, backends=None):
        tasks = self._prepare_run_for_dataset(dataset, executor, roi, corrections, backends)
        cancel_id = str(uuid.uuid4())
        self._debug_task_pickling(tasks)

        if progress:
            t = tqdm.tqdm(total=len(tasks))
        for part_results, task in executor.run_tasks(tasks, cancel_id):
            if progress:
                t.update(1)
            for results, udf in zip(part_results, self._udfs):
                udf.set_views_for_partition(task.partition)
                udf.merge(
                    dest=udf.results.get_proxy(),
                    src=results.get_proxy()
                )

        if progress:
            t.close()
        for udf in self._udfs:
            udf.clear_views()

        return [
            udf.results.as_dict()
            for udf in self._udfs
        ]

    async def run_for_dataset_async(
            self, dataset: DataSet, executor, cancel_id, roi=None, corrections=None, backends=None):
        tasks = self._prepare_run_for_dataset(dataset, executor, roi, corrections, backends)

        async for part_results, task in executor.run_tasks(tasks, cancel_id):
            for results, udf in zip(part_results, self._udfs):
                udf.set_views_for_partition(task.partition)
                udf.merge(
                    dest=udf.results.get_proxy(),
                    src=results.get_proxy()
                )
                udf.clear_views()
            yield tuple(
                udf.results.as_dict()
                for udf in self._udfs
            )
        else:
            # yield at least one result (which should be empty):
            for udf in self._udfs:
                udf.clear_views()
            yield tuple(
                udf.results.as_dict()
                for udf in self._udfs
            )

    def _roi_for_partition(self, roi, partition: Partition):
        return roi.reshape(-1)[partition.slice.get(nav_only=True)]

    def _make_udf_tasks(self, dataset: DataSet, roi, corrections, backends):
        for idx, partition in enumerate(dataset.get_partitions()):
            if roi is not None:
                roi_for_part = self._roi_for_partition(roi, partition)
                if np.count_nonzero(roi_for_part) == 0:
                    # roi is empty for this partition, ignore
                    continue
            udfs = [
                udf.copy_for_partition(partition, roi)
                for udf in self._udfs
            ]
            yield UDFTask(
                partition=partition, idx=idx, udfs=udfs, roi=roi, corrections=corrections,
                backends=backends,
            )
