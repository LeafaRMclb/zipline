from functools import total_ordering
from six import (
    iteritems,
    with_metaclass,
)
from weakref import WeakKeyDictionary

from toolz import first

from zipline.pipeline.classifiers import Classifier, Latest as LatestClassifier
from zipline.pipeline.domain import Domain, GENERIC
from zipline.pipeline.factors import Factor, Latest as LatestFactor
from zipline.pipeline.filters import Filter, Latest as LatestFilter
from zipline.pipeline.sentinels import NotSpecified, sentinel
from zipline.pipeline.term import (
    AssetExists,
    LoadableTerm,
    validate_dtype,
)
from zipline.utils.input_validation import ensure_dtype, expect_types
from zipline.utils.numpy_utils import NoDefaultMissingValue
from zipline.utils.preprocess import preprocess


IsSpecialization = sentinel('IsSpecialization')


class Column(object):
    """
    An abstract column of data, not yet associated with a dataset.
    """
    @preprocess(dtype=ensure_dtype)
    def __init__(self,
                 dtype,
                 missing_value=NotSpecified,
                 doc=None,
                 metadata=None):
        self.dtype = dtype
        self.missing_value = missing_value
        self.doc = doc
        self.metadata = metadata.copy() if metadata is not None else {}

    def bind(self, name):
        """
        Bind a `Column` object to its name.
        """
        return _BoundColumnDescr(
            dtype=self.dtype,
            missing_value=self.missing_value,
            name=name,
            doc=self.doc,
            metadata=self.metadata,
        )


class _BoundColumnDescr(object):
    """
    Intermediate class that sits on `DataSet` objects and returns memoized
    `BoundColumn` objects when requested.

    This exists so that subclasses of DataSets don't share columns with their
    parent classes.
    """
    def __init__(self, dtype, missing_value, name, doc, metadata):
        # Validating and calculating default missing values here guarantees
        # that we fail quickly if the user passes an unsupporte dtype or fails
        # to provide a missing value for a dtype that requires one
        # (e.g. int64), but still enables us to provide an error message that
        # points to the name of the failing column.
        try:
            self.dtype, self.missing_value = validate_dtype(
                termname="Column(name={name!r})".format(name=name),
                dtype=dtype,
                missing_value=missing_value,
            )
        except NoDefaultMissingValue:
            # Re-raise with a more specific message.
            raise NoDefaultMissingValue(
                "Failed to create Column with name {name!r} and"
                " dtype {dtype} because no missing_value was provided\n\n"
                "Columns with dtype {dtype} require a missing_value.\n"
                "Please pass missing_value to Column() or use a different"
                " dtype.".format(dtype=dtype, name=name)
            )
        self.name = name
        self.doc = doc
        self.metadata = metadata

    def __get__(self, instance, owner):
        """
        Produce a concrete BoundColumn object when accessed.

        We don't bind to datasets at class creation time so that subclasses of
        DataSets produce different BoundColumns.
        """
        return BoundColumn(
            dtype=self.dtype,
            missing_value=self.missing_value,
            dataset=owner,
            name=self.name,
            doc=self.doc,
            metadata=self.metadata,
        )


class BoundColumn(LoadableTerm):
    """
    A column of data that's been concretely bound to a particular dataset.

    Instances of this class are dynamically created upon access to attributes
    of DataSets (for example, USEquityPricing.close is an instance of this
    class).

    Attributes
    ----------
    dtype : numpy.dtype
        The dtype of data produced when this column is loaded.
    latest : zipline.pipeline.data.Factor or zipline.pipeline.data.Filter
        A Filter, Factor, or Classifier computing the most recently known value
        of this column on each date.

        Produces a Filter if self.dtype == ``np.bool_``.
        Produces a Classifier if self.dtype == ``np.int64``
        Otherwise produces a Factor.
    dataset : zipline.pipeline.data.DataSet
        The dataset to which this column is bound.
    name : str
        The name of this column.
    metadata : dict
        Extra metadata associated with this column.
    """
    mask = AssetExists()
    window_safe = True

    def __new__(cls,
                dtype,
                missing_value,
                dataset,
                name,
                doc,
                metadata):
        return super(BoundColumn, cls).__new__(
            cls,
            domain=dataset.domain,
            dtype=dtype,
            missing_value=missing_value,
            dataset=dataset,
            name=name,
            ndim=dataset.ndim,
            doc=doc,
            metadata=metadata,
        )

    def _init(self, dataset, name, doc, metadata, *args, **kwargs):
        self._dataset = dataset
        self._name = name
        self.__doc__ = doc
        self._metadata = metadata
        return super(BoundColumn, self)._init(*args, **kwargs)

    @classmethod
    def _static_identity(cls, dataset, name, doc, metadata, *args, **kwargs):
        return (
            super(BoundColumn, cls)._static_identity(*args, **kwargs),
            dataset,
            name,
            doc,
            frozenset(sorted(metadata.items(), key=first)),
        )

    def specialize(self, domain):
        """Specialize ``self`` to a concrete domain.
        """
        if domain == self.domain:
            return self

        return type(self)(
            dtype=self.dtype,
            missing_value=self.missing_value,
            dataset=self._dataset.specialize(domain),
            name=self._name,
            doc=self.__doc__,
            metadata=self._metadata,
        )

    def unspecialize(self):
        """
        Unspecialize a column to its generic form.

        This is equivalent to ``column.specialize(GENERIC)``.
        """
        return self.specialize(GENERIC)

    @property
    def dataset(self):
        """
        The dataset to which this column is bound.
        """
        return self._dataset

    @property
    def name(self):
        """
        The name of this column.
        """
        return self._name

    @property
    def metadata(self):
        """
        A copy of the metadata for this column.
        """
        return self._metadata.copy()

    @property
    def qualname(self):
        """
        The fully-qualified name of this column.

        Generated by doing '.'.join([self.dataset.__name__, self.name]).
        """
        return '.'.join([self.dataset.qualname, self.name])

    @property
    def latest(self):
        dtype = self.dtype
        if dtype in Filter.ALLOWED_DTYPES:
            Latest = LatestFilter
        elif dtype in Classifier.ALLOWED_DTYPES:
            Latest = LatestClassifier
        else:
            assert dtype in Factor.ALLOWED_DTYPES, "Unknown dtype %s." % dtype
            Latest = LatestFactor

        return Latest(
            inputs=(self,),
            dtype=dtype,
            missing_value=self.missing_value,
            ndim=self.ndim,
        )

    def __repr__(self):
        return "{qualname}::{dtype}".format(
            qualname=self.qualname,
            dtype=self.dtype.name,
        )

    def graph_repr(self):
        """Short repr to use when rendering Pipeline graphs."""
        # Graphviz interprets `\l` as "divide label into lines, left-justified"
        return "BoundColumn:\\l  Dataset: {}\\l  Column: {}\\l".format(
            self.dataset.__name__,
            self.name
        )

    def recursive_repr(self):
        """Short repr used to render in recursive contexts."""
        return self.qualname


@total_ordering
class DataSetMeta(type):
    """
    Metaclass for DataSets

    Supplies name and dataset information to Column attributes, and manages
    families of specialized dataset.
    """
    def __new__(mcls, name, bases, dict_):
        if len(bases) != 1:
            # Disallowing multiple inheritance makes it easier for us to
            # determine whether a given dataset is the root for its family of
            # specializations.
            raise TypeError("Multiple dataset inheritance is not supported.")

        # This marker is set in the class dictionary by `specialize` below.
        is_specialization = dict_.pop(IsSpecialization, False)

        newtype = super(DataSetMeta, mcls).__new__(mcls, name, bases, dict_)

        if not isinstance(newtype.domain, Domain):
            raise TypeError(
                "Expected a Domain for {}.domain, but got {} instead.".format(
                    newtype.__name__,
                    type(newtype.domain),
                )
            )

        # Collect all of the column names that we inherit from our parents.
        column_names = set().union(
            *(getattr(base, '_column_names', ()) for base in bases)
        )

        # Collect any new columns from this dataset.
        for maybe_colname, maybe_column in iteritems(dict_):
            if isinstance(maybe_column, Column):
                # add column names defined on our class
                bound_column_descr = maybe_column.bind(maybe_colname)
                setattr(newtype, maybe_colname, bound_column_descr)
                column_names.add(maybe_colname)

        newtype._column_names = frozenset(column_names)

        if not is_specialization:
            # This is the new root of a family of specializations. Store the
            # memoized dictionary for family on this type.
            newtype._domain_specializations = WeakKeyDictionary({
                newtype.domain: newtype,
            })

        return newtype

    @expect_types(domain=Domain)
    def specialize(self, domain):
        """
        Specialize a generic DataSet to a concrete domain.

        Parameters
        ----------
        domain : zipline.pipeline.domain.Domain
            Domain to which we should generate a specialization.

        Returns
        -------
        specialized : DataSetMeta
            A new DataSet subclass with the same columns as ``self``, but
            specialized to ``domain``.
        """
        # We're already the specialization to this domain, so just return self.
        if domain == self.domain:
            return self

        try:
            return self._domain_specializations[domain]
        except KeyError:
            if not self._can_create_new_specialization(domain):
                # This either means we're already a specialization and trying
                # to create a new specialization, or we're the generic version
                # of a root-specialized dataset, which we don't want to create
                # new specializations of.
                raise ValueError(
                    "Can't specialize {dataset} to new domain {new}.".format(
                        dataset=self.__name__,
                        current=self.domain,
                        new=domain,
                    )
                )
            new_type = self._create_specialization(domain)
            self._domain_specializations[domain] = new_type
            return new_type

    def unspecialize(self):
        """
        Unspecialize a dataset to its generic form.

        This is equivalent to ``dataset.specialize(GENERIC)``.
        """
        return self.specialize(GENERIC)

    def _can_create_new_specialization(self, domain):
        # Always allow specializing to a generic domain.
        if domain is GENERIC:
            return True
        elif '_domain_specializations' in vars(self):
            # This branch is True if we're the root of a family.
            # Allow specialization if we're generic.
            return self.domain is GENERIC
        else:
            # If we're not the root of a family, we can't create any new
            # specializations.
            return False

    def _create_specialization(self, domain):
        # These are all assertions because we should have handled these cases
        # already in specialize().
        assert isinstance(domain, Domain)
        assert domain not in self._domain_specializations, (
            "Domain specializations should be memoized!"
        )
        if domain is not GENERIC:
            assert self.domain is GENERIC, (
                "Can't specialize dataset with domain {} to domain {}.".format(
                    self.domain, domain,
                )
            )

        # Create a new subclass of ``self`` with the given domain.
        # Mark that it's a specialization so that we know not to create a new
        # family for it.
        name = self.__name__
        bases = (self,)
        dict_ = {'domain': domain, IsSpecialization: True}
        return type(name, bases, dict_)

    @property
    def columns(self):
        return frozenset(
            getattr(self, colname) for colname in self._column_names
        )

    @property
    def qualname(self):
        if self.domain is GENERIC:
            specialization_key = ''
        else:
            specialization_key = '<' + self.domain.country_code + '>'

        return self.__name__ + specialization_key

    def __lt__(self, other):
        return id(self) < id(other)

    def __repr__(self):
        return '<DataSet: %r, domain=%s>' % (self.__name__, self.domain)


class DataSet(with_metaclass(DataSetMeta, object)):
    """
    Base class for Pipeline datasets.

    A DataSet has two parts:

    1. A collection of :class:`~zipline.pipeline.data.Column` objects that
       describe the attributes of the dataset.

    2. A :class:`~zipline.pipeline.domain.Domain` describing the assets and
       calendar of the data represented by the DataSet.

    To create a new Pipeline dataset, define a subclass of DataSet and set one
    or more Column objects as class-level attributes. Each column requires a
    ``np.dtype`` that describes the type of data that should be produced by a
    loader for the dataset. Integer columns must also provide a "missing value"
    to be used when no value is available for a given asset/date combination.

    By default, the domain of a dataset is the special singleton value GENERIC,
    which means that they can be used in a Pipeline running on **any** domain.

    In some cases, it may be preferable to restrict a dataset to only allow
    support a single domain. For example, a DataSet may describe data from a
    vendor that only covers the US. To restrict a dataset to a specific domain,
    define a `domain` attribute at class scope.

    You can also define a domain-specific version of a generic DataSet by
    calling its `specialize` method with the domain of interest.

    Examples
    --------
    The built-in EquityPricing dataset is defined as follows::

        class EquityPricing(DataSet):
            open = Column(float)
            high = Column(float)
            low = Column(float)
            close = Column(float)
            volume = Column(float)

    The built-in USEquityPricing dataset is a specialization of
    EquityPricing. It is defined as::

        from zipline.pipeline.domain import US_EQUITIES
        USEquityPricing = EquityPricing.specialize(US_EQUITIES)

    Columns can have types other than float. A dataset containing assorted
    company metadata might be defined like this::

        class CompanyMetadata(DataSet):
            # Use float for semantically-numeric data, even if it's always
            # integral valued (see Notes section below). The default missing
            # value for floats is NaN.
            shares_outstanding = Column(float)

            # Use object for string columns. The default missing value for
            # object-dtype columns is None.
            ticker = Column(object)

            # Use integers for integer-valued categorical data like sector or
            # industry codes. Integer-dtype columns require an explicit missing
            # value.
            sector_code = Column(int, missing_value=-1)

            # Use bool for boolean-valued flags. Note that the default missing
            # value for bool-dtype columns is False.
            is_primary_share = Column(bool)

    Notes
    -----
    Because numpy has no native support for integers with missing values, users
    are strongly encouraged to use floats for any data that's semantically
    numeric. Doing so enables the use of `NaN` as a natural missing value,
    which has useful propagation semantics.
    """
    domain = GENERIC
    ndim = 2


# This attribute is set by DataSetMeta to mark that a class is the root of a
# family of datasets with diffent domains. We don't want that behavior for the
# base DataSet class, and we also don't want to accidentally use a shared
# version of this attribute if we fail to set this in a subclass somewhere.
del DataSet._domain_specializations
