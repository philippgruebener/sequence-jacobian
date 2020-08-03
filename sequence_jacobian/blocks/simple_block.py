import numpy as np
import numbers
from numba import njit

from .. import utils
from .. import asymptotic

'''Part 1: SimpleBlock class and @simple decorator to generate it'''


def simple(f):
    return SimpleBlock(f)


class SimpleBlock:
    """Generated from simple block written in Dynare-ish style and decorated with @simple, e.g.
    
    @simple
    def production(Z, K, L, alpha):
        Y = Z * K(-1) ** alpha * L ** (1 - alpha)
        return Y

    which is a SimpleBlock that takes in Z, K, L, and alpha, all of which can be either constants
    or series, and implements a Cobb-Douglas production function, noting that for production today
    we use the capital K(-1) determined yesterday.
    
    Key methods are .ss, .td, and .jac, like HetBlock.
    """

    def __init__(self, f):
        self.f = f
        self.input_list = utils.input_list(f)
        self.output_list = utils.output_list(f)
        self.inputs = set(self.input_list)
        self.outputs = set(self.output_list)

    def __repr__(self):
        return f"<SimpleBlock '{self.f.__name__}'>"

    def ss(self, *args, **kwargs):
        # Wrap args and kwargs in Ignore/IgnoreVector classes to be passed into the function "f"
        args = [ignore(x) for x in args]
        kwargs = {k: ignore(v) for k, v in kwargs.items()}

        # Impose the return arguments are numeric primitives (float or np.ndarray) and not Ignore/IgnoreVector types
        if len(self.output_list) > 1:
            return tuple([numeric_primitive(o) for o in self.f(*args, **kwargs)])
        else:
            return numeric_primitive(self.f(*args, **kwargs))

    def td(self, ss, **kwargs):
        kwargs_new = {}
        for k, v in kwargs.items():
            if np.isscalar(v):
                raise ValueError(f'Keyword argument {k}={v} is scalar, should be time path.')
            kwargs_new[k] = Displace(v, ss=ss.get(k, None), name=k)

        for k in self.input_list:
            if k not in kwargs_new:
                kwargs_new[k] = Ignore(ss[k])

        if len(self.output_list) > 1:
            return dict(zip(self.output_list, utils.make_tuple([numeric_primitive(o) for o in self.f(**kwargs_new)])))
        else:
            return dict(zip(self.output_list, numeric_primitive(self.f(**kwargs_new))))

    def jac(self, ss, T=None, shock_list=None, h=1E-5):
        """Assemble nested dict of Jacobians

        Parameters
        ----------
        ss : dict,
            steady state values
        T : int, optional
            number of time periods for explicit T*T Jacobian
            if omitted, more efficient SimpleSparse objects returned
        shock_list : list of str, optional
            names of input variables to differentiate wrt; if omitted, assume all inputs
        h : float, optional
            radius for symmetric numerical differentiation

        Returns
        -------
        J : dict of {str: dict of {str: array(T,T)}}
            J[o][i] for output o and input i gives Jacobian of o with respect to i
            This Jacobian is a SimpleSparse object or, if T specific, a T*T matrix, omitted by convention
            if zero
        """
        if shock_list is None:
            shock_list = self.input_list

        raw_derivatives = {o: {} for o in self.output_list}

        # initialize dict of default inputs k on which we'll evaluate simple blocks
        # each element is 'Ignore' object containing ss value of input k that ignores
        # time displacement, i.e. k(3) in a simple block will evaluate to just ss k
        x_ss_new = {k: Ignore(ss[k]) for k in self.input_list}

        # loop over all inputs k which we want to differentiate
        for k in shock_list:
            # detect all non-zero time displacements i with which k(i) appears in f
            # wrap steady-state values in Reporter class (similar to Ignore but adds any time
            # displacements to shared set), then feed into f
            reporter = Reporter(ss[k])
            x_ss_new[k] = reporter
            self.f(**x_ss_new)
            relevant_displacements = reporter.myset

            # add zero by default (Reporter can't detect, since no explicit call k(0) required)
            relevant_displacements.add(0)

            # evaluate derivative with respect to input at each displacement i
            for i in relevant_displacements:
                # perturb k(i) up by +h from steady state and evaluate f
                x_ss_new[k] = Perturb(ss[k], h, i)
                y_up_all = utils.make_tuple(self.f(**x_ss_new))

                # perturb k(i) down by -h from steady state and evaluate f
                x_ss_new[k] = Perturb(ss[k], -h, i)
                y_down_all = utils.make_tuple(self.f(**x_ss_new))

                # for each output o of f, if affected, store derivative in rawderivatives[o][k][i]
                # this builds up Jacobian rawderivatives[o][k] of output o with respect to input k
                # which is 'sparsederiv' dict mapping time displacements 'i' to derivatives
                for y_up, y_down, o in zip(y_up_all, y_down_all, self.output_list):
                    if y_up != y_down:
                        sparsederiv = raw_derivatives[o].setdefault(k, {})
                        sparsederiv[i] = (y_up - y_down) / (2 * h)
            
            # replace our Perturb object for k with Ignore object, so we can run with other k
            x_ss_new[k] = Ignore(ss[k])

        # process raw_derivatives to return either SimpleSparse objects or (if T provided) matrices
        J = {o: {} for o in self.output_list}
        for o in self.output_list:
            for k in raw_derivatives[o].keys():
                if T is None:
                    J[o][k] = SimpleSparse.from_simple_diagonals(raw_derivatives[o][k])
                else:
                    J[o][k] = SimpleSparse.from_simple_diagonals(raw_derivatives[o][k]).matrix(T)

        return J


'''Part 2: SimpleSparse class to represent and work with sparse Jacobians of SimpleBlocks'''


class SimpleSparse:
    """Efficient representation of sparse linear operators, which are linear combinations of basis
    operators represented by pairs (i, m), where i is the index of diagonal on which there are 1s
    (measured by # above main diagonal) and m is number of initial entries missing.

    Examples of such basis operators:
        - (0, 0) is identity operator
        - (0, 2) is identity operator with first two '1's on main diagonal missing
        - (1, 0) has 1s on diagonal above main diagonal: "left-shift" operator
        - (-1, 1) has 1s on diagonal below main diagonal, except first column

    The linear combination of these basis operators that makes up a given SimpleSparse object is
    stored as a dict 'elements' mapping (i, m) -> x.

    The Jacobian of a SimpleBlock is a SimpleSparse operator combining basis elements (i, 0). We need
    the more general basis (i, m) to ensure closure under multiplication.

    These (i, m) correspond to the Q_(-i, m) operators defined for Proposition 2 of the Sequence Space
    Jacobian paper. The flipped sign in the code is so that the index 'i' matches the k(i) notation
    for writing SimpleBlock functions.

    The "dunder" methods x.__add__(y), x.__matmul__(y), x.__rsub__(y), etc. in Python implement infix
    operations x + y, x @ y, y - x, etc. Defining these allows us to use these more-or-less
    interchangeably with ordinary NumPy matrices.
    """

    # when performing binary operations on SimpleSparse and a NumPy array, use SimpleSparse's rules
    __array_priority__ = 1000

    def __init__(self, elements):
        self.elements = elements
        self.indices, self.xs = None, None

    @staticmethod
    def from_simple_diagonals(elements):
        """Take dict i -> x, i.e. from SimpleBlock differentiation, convert to SimpleSparse (i, 0) -> x"""
        return SimpleSparse({(i, 0): x for i, x in elements.items()})

    def matrix(self, T):
        """Return matrix giving first T rows and T columns of matrix representation of SimpleSparse"""
        return self + np.zeros((T, T))

    def array(self):
        """Rewrite dict (i, m) -> x as pair of NumPy arrays, one size-N*2 array of ints with rows (i, m)
        and one size-N array of floats with entries x.

        This is needed for Numba to take as input. Cache for efficiency.
        """
        if self.indices is not None:
            return self.indices, self.xs
        else:
            indices, xs = zip(*self.elements.items())
            self.indices, self.xs = np.array(indices), np.array(xs)
            return self.indices, self.xs

    @property
    def asymptotic_time_invariant(self):
        indices, xs = self.array()
        tau = np.max(np.abs(indices[:, 0]))+1 # how far out do we go?
        v = np.zeros(2*tau-1)
        #v[indices[:, 0]+tau-1] = xs
        v[-indices[:, 0]+tau-1] = xs # switch from asymptotic ROW to asymptotic COLUMN
        return asymptotic.AsymptoticTimeInvariant(v)

    @property
    def T(self):
        """Transpose"""
        return SimpleSparse({(-i, m): x for (i, m), x in self.elements.items()})

    def __pos__(self):
        return self

    def __neg__(self):
        return SimpleSparse({im: -x for im, x in self.elements.items()})

    def __matmul__(self, A):
        if isinstance(A, SimpleSparse):
            # multiply SimpleSparse by SimpleSparse, simple analytical rules in multiply_rs_rs
            return multiply_rs_rs(self, A)
        elif isinstance(A, np.ndarray):
            # multiply SimpleSparse by matrix or vector, multiply_rs_matrix uses slicing
            indices, xs = self.array()
            if A.ndim == 2:
                return multiply_rs_matrix(indices, xs, A)
            elif A.ndim == 1:
                return multiply_rs_matrix(indices, xs, A[:, np.newaxis])[:, 0]
            else:
                return NotImplemented
        else:
            return NotImplemented

    def __rmatmul__(self, A):
        # multiplication rule when this object is on right (will only be called when left is matrix)
        # for simplicity, just use transpose to reduce this to previous cases
        return (self.T @ A.T).T

    def __add__(self, A):
        if isinstance(A, SimpleSparse):
            # add SimpleSparse to SimpleSparse, combining dicts, summing x when (i, m) overlap
            elements = self.elements.copy()
            for im, x in A.elements.items():
                if im in elements:
                    elements[im] += x
                    # safeguard to retain sparsity: disregard extremely small elements (num error)
                    if abs(elements[im]) < 1E-14:
                        del elements[im]
                else:
                    elements[im] = x
            return SimpleSparse(elements)
        else:
            # add SimpleSparse to T*T matrix
            if not isinstance(A, np.ndarray) or A.ndim != 2 or A.shape[0] != A.shape[1]:
                return NotImplemented
            T = A.shape[0]

            # fancy trick to do this efficiently by writing A as flat vector
            # then (i, m) can be mapped directly to NumPy slicing!
            A = A.flatten()     # use flatten, not ravel, since we'll modify A and want a copy
            for (i, m), x in self.elements.items():
                if i < 0:
                    A[T * (-i) + (T + 1) * m::T + 1] += x
                else:
                    A[i + (T + 1) * m:(T - i) * T:T + 1] += x
            return A.reshape((T, T))

    def __radd__(self, A):
        try:
            return self + A
        except:
            print(self)
            print(A)
            raise

    def __sub__(self, A):
        # slightly inefficient implementation with temporary for simplicity
        return self + (-A)

    def __rsub__(self, A):
        return -self + A

    def __mul__(self, a):
        if not np.isscalar(a):
            return NotImplemented
        return SimpleSparse({im: a * x for im, x in self.elements.items()})

    def __rmul__(self, a):
        return self * a

    def __repr__(self):
        formatted = '{' + ', '.join(f'({i}, {m}): {x:.3f}' for (i, m), x in self.elements.items()) + '}'
        return f'SimpleSparse({formatted})'

    def __eq__(self, s):
        return self.elements == s.elements


def multiply_basis(t1, t2):
    """Matrix multiplication operation mapping two sparse basis elements to another."""
    # equivalent to formula in Proposition 2 of Sequence Space Jacobian paper, but with
    # signs of i and j flipped to reflect different sign convention used here
    i, m = t1
    j, n = t2
    k = i + j
    if i >= 0:
        if j >= 0:
            l = max(m, n - i)
        elif k >= 0:
            l = max(m, n - k)
        else:
            l = max(m + k, n)
    else:
        if j <= 0:
            l = max(m + j, n)
        else:
            l = max(m, n) + min(-i, j)
    return k, l


def multiply_rs_rs(s1, s2):
    """Matrix multiplication operation on two SimpleSparse objects."""
    # iterate over all pairs (i, m) -> x and (j, n) -> y in objects,
    # add all pairwise products to get overall product
    elements = {}
    for im, x in s1.elements.items():
        for jn, y in s2.elements.items():
            kl = multiply_basis(im, jn)
            if kl in elements:
                elements[kl] += x * y
            else:
                elements[kl] = x * y
    return SimpleSparse(elements)


@njit
def multiply_rs_matrix(indices, xs, A):
    """Matrix multiplication of SimpleSparse object ('indices' and 'xs') and matrix A.
    Much more computationally demanding than multiplying two SimpleSparse (which is almost
    free with simple analytical formula), so we implement as jitted function."""
    n = indices.shape[0]
    T = A.shape[0]
    S = A.shape[1]
    Aout = np.zeros((T, S))

    for count in range(n):
        # for Numba to jit easily, SimpleSparse with basis elements '(i, m)' with coefs 'x'
        # was stored in 'indices' and 'xs'
        i = indices[count, 0]
        m = indices[count, 1]
        x = xs[count]

        # loop faster than vectorized when jitted
        # directly use def of basis element (i, m), displacement of i and ignore first m
        if i == 0:
            for t in range(m, T):
                for s in range(S):
                    Aout[t, s] += x * A[t, s]
        elif i > 0:
            for t in range(m, T - i):
                for s in range(S):
                    Aout[t, s] += x * A[t + i, s]
        else:
            for t in range(m - i, T):
                for s in range(S):
                    Aout[t, s] += x * A[t + i, s]
    return Aout


'''Part 3: helper classes used by SimpleBlock for .ss, .td, and .jac evaluation'''


def ignore(x):
    if isinstance(x, numbers.Real):
        return Ignore(x)
    elif isinstance(x, np.ndarray):
        return IgnoreVector(x)
    else:
        raise TypeError(f"{type(x)} is not supported. Must provide either a float or an nd.array as an argument")


class Ignore(float):
    """This class ignores time displacements of a scalar.
    Standard arithmetic operators including +, -, x, /, ** all overloaded to "promote" the result of
    any arithmetic operation with an Ignore type to an Ignore type. e.g. type(Ignore(1) + 1) is Ignore
    """

    def __call__(self, index):
        return self


class IgnoreVector(np.ndarray):
    """This class ignores time displacements of a np.ndarray.
       See NumPy documentation on "Subclassing ndarray" for more details on the use of __new__
       for this implementation.
       Operators overloaded similarly to the analogous scalar version of this class, Ignore."""

    def __new__(cls, x):
        obj = np.asarray(x).view(cls)
        return obj

    def __call__(self, index):
        return self


class Displace(np.ndarray):
    """This class makes time displacements of a time path, given the steady-state value.
    Needed for SimpleBlock.td()"""

    def __new__(cls, x, ss=None, name='UNKNOWN'):
        obj = np.asarray(x).view(cls)
        obj.ss = ss
        obj.name = name
        return obj

    def __call__(self, index):
        if index != 0:
            if self.ss is None:
                raise KeyError(f'Trying to call {self.name}({index}), but steady-state {self.name} not given!')
            newx = np.full(len(self), self.ss)
            if index > 0:
                newx[:-index] = numeric_primitive(self)[index:]
            else:
                newx[-index:] = numeric_primitive(self)[:index]
            return Displace(newx, ss=self.ss)
        else:
            return self


class Reporter(float):
    """This class adds to a shared set to tell us what x[i] are accessed.
    Needed for differentiation in SimpleBlock.jac()"""

    def __init__(self, value):
        self.myset = set()

    def __call__(self, index):
        self.myset.add(index)
        return self


class Perturb(float):
    """This class uses the shared set to perturb each x[i] separately, starting at steady-state values.
    Needed for differentiation in SimpleBlock.jac()"""

    def __new__(cls, value, h, index):
        if index == 0:
            return float.__new__(cls, value + h)
        else:
            return float.__new__(cls, value)

    def __init__(self, value, h, index):
        self.h = h
        self.index = index

    def __call__(self, index):
        if self.index == 0:
            if index == 0:
                return self
            else:
                return self - self.h
        else:
            if self.index == index:
                return self + self.h
            else:
                return self


def numeric_primitive(instance):
    # If it is already a primitive, just return it
    if type(instance) in {int, float, np.ndarray}:
        return instance
    else:
        return instance.real if issubclass(type(instance), numbers.Real) else instance.base


# Assumes "op" is an actual well-defined arithmetic operator. If necessary, implement more stringent checks
# on the "op" being passed in so nonsense doesn't come out.
# i.e. reverse_op("__round__") doesn't return reverse_op("__ound__")
def reverse_op(op):
    if op[2] == "r":
        return op[0:2] + op[3:]
    else:
        return op[0:2] + "r" + op[2:]


def apply_op(op, *args):
    if len(args) == 1:
        return apply_unary_op(op, *args)
    elif len(args) == 2:
        return apply_binary_op(op, *args)
    else:
        raise ValueError(f"apply_op only supports unary or binary operators currently. {len(args)} is an invalid"
                         f" number of arguments to provide.")


def apply_unary_op(op, a):
    return getattr(a, op)()


def apply_binary_op(op, a1, a2):
    if getattr(a1, op)(a2) is not NotImplemented:
        return getattr(a1, op)(a2)
    elif getattr(a2, reverse_op(op))(a1) is not NotImplemented:
        return getattr(a2, reverse_op(op))(a1)
    else:
        raise NotImplementedError(f"{op} cannot be performed between {a1} and {a2} directly, and no"
                                  f" valid reverse operation exists either.")


def apply_unary_op_to_primitives(op, a):
    return apply_unary_op(op, numeric_primitive(a))


def apply_binary_op_to_primitives(op, a1, a2):
    a1_p = numeric_primitive(a1)
    a2_p = numeric_primitive(a2)
    return apply_binary_op(op, a1_p, a2_p)


# The following lines overload the standard arithmetic operators of Ignore to return an Ignore type as opposed to
# following the standard promotion behavior.
def _overload_operator(constructor, op, customize_attributes=None, **constructor_kwargs):
    """Overload the provided operator for a given class (that is a child class of a parent class
    having well-defined behavior for the provided set of operators) to return an instance of the child class.
    e.g. type(Ignore(1) + 1) is Ignore, if overload_operators is used to overload __add__ for the class Ignore.

    constructor: `str`
        The name of the class whose operators we want to overload or a constructor for this class
    op: `string`
        A `str` of the name of the operators, e.g. "__add__", that we hope to overload
    customize_attributes: `function` or None
        If provided, a function that maps the operator and self (and other if it is a binary operator)
        to additional attributes desired in constructing a new instance of an object of type(self)
        e.g. since Displace objects have a .ss attribute, customize_attributes can map the operator "__add__",
        self, and other to a resulting Displace object with a valid .ss attribute
    """
    if op in {"__pos__", "__neg__"}:
        if customize_attributes is not None:
            return lambda self: constructor(apply_unary_op_to_primitives(op, self),
                                            **{**customize_attributes(op, self), **constructor_kwargs})
        else:
            return lambda self: constructor(apply_unary_op_to_primitives(op, self), **constructor_kwargs)
    else:
        if customize_attributes is not None:
            return lambda self, other: constructor(apply_binary_op_to_primitives(op, self, other),
                                                   **{**customize_attributes(op, self, other), **constructor_kwargs})
        else:
            return lambda self, other: constructor(apply_binary_op_to_primitives(op, self, other),
                                                   **constructor_kwargs)


def overload_operators(custom_class, operators, customize_attributes=None, constructor=None, **constructor_kwargs):
    """Overload the provided set of operators for a given class (that is a child class of a parent class
    having well-defined behavior for the provided set of operators) to return an instance of the child class.
    e.g. type(Ignore(1) + 1) is Ignore, if overload_operators is used to overload __add__ for the class Ignore.

    See docstring for _overload_operator for more details on arguments
    """
    for op in operators:
        if constructor is None:
            setattr(custom_class, op, _overload_operator(custom_class, op, customize_attributes=customize_attributes,
                                                         **constructor_kwargs))
        else:
            setattr(custom_class, op, _overload_operator(constructor, op, customize_attributes=customize_attributes,
                                                         **constructor_kwargs))


def override_default_promotion(primary_class, overriding_class, op,
                               primary_class_constructor=None, overriding_class_constructor=None,
                               primary_kwargs={}, override_kwargs={}):
    """
    Override the default promotion class when applying primary_class.op(overriding_class) from primary_class to
    overriding_class.
    e.g. Want Ignore(1) + Displace(np.array([1,2,3]), ss=2) = Displace(np.array([2,3,4]), ss=3) as opposed to
    IgnoreVector(np.array([2,3,4])).

    primary_class: `str`
        The class, whose operation we are modifying to override its default promotion class
    overriding_class: `str`
        The class, which we impose is returned from applying the operation as opposed to the default promotion class
    op: `str`
        The operation being overriden

    primary_class_constructor: `str` or None
        Optional alternative constructor method. If None then will use the default constructor provided by primary_class
    overriding_class_constructor: `str` or None
        Optional alternative constructor method. If None then will use the default constructor provided by overriding_class
    primary_kwargs: dict
        The keyword arguments to provide to the primary class constructor
    override_kwargs: dict
        The keyword arguments to be provided to the overriding class constructor
    return: `op_override`, a function overriding the operator to be provided to setattr
    """
    def op_override(self, other):
        if isinstance(self, primary_class) and isinstance(other, overriding_class):
            constructor = overriding_class if overriding_class_constructor is None else overriding_class_constructor
            return _overload_operator(constructor, reverse_op(op), **override_kwargs)(other, self)
        else:
            constructor = primary_class if primary_class_constructor is None else primary_class_constructor
            return _overload_operator(constructor, op, **primary_kwargs)(self, other)
    return op_override


def override_default_promotions(primary_class, overriding_class, operators,
                                primary_class_constructor=None, overriding_class_constructor=None, **kwargs):
    """
    Override the default promotion class when applying primary_class.op(overriding_class) from primary_class to
    overriding_class.

    For details about the arguments and potential keyword arguments to be provided,
    see docstring for override_default_promotion.
    """
    for op in operators:
        setattr(primary_class, op, override_default_promotion(primary_class, overriding_class, op,
                                                              primary_class_constructor=primary_class_constructor,
                                                              overriding_class_constructor=overriding_class_constructor,
                                                              **kwargs))


# Overload operators on classes Ignore, IgnoreVector, Displace, Reporter, and Perturb
unary_operators = ["__neg__", "__pos__"]
binary_operators = ["__add__", "__sub__", "__mul__", "__truediv__", "__pow__",
                    "__radd__", "__rsub__", "__rmul__", "__rtruediv__", "__rpow__"]
operators = unary_operators + binary_operators

# Custom attribute functions
# Assume arg is either empty or of size 1 (so either a unary of binary operator)
def compute_displace_attributes(op, self, *arg):
    if not arg:
        return {"ss": apply_unary_op_to_primitives(op, self.ss)}
    else:
        if isinstance(arg[0], Displace):
            return {"ss": apply_binary_op_to_primitives(op, self.ss, arg[0].ss)}
        else:
            return {"ss": apply_binary_op_to_primitives(op, self.ss, arg[0])}


overload_operators(Ignore, operators, constructor=ignore)
overload_operators(IgnoreVector, operators, constructor=ignore)
overload_operators(Displace, ["__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__",
                   "__truediv__", "__rtruediv__", "__pow__", "__rpow__", "__neg__", "__pos__"],
                   customize_attributes=compute_displace_attributes)

override_default_promotions(Ignore, Displace, binary_operators, primary_class_constructor=ignore,
                            override_kwargs={"customize_attributes": compute_displace_attributes})
override_default_promotions(IgnoreVector, Displace, binary_operators, primary_class_constructor=ignore,
                            override_kwargs={"customize_attributes": compute_displace_attributes})
