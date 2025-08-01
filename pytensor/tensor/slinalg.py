import logging
import warnings
from collections.abc import Sequence
from functools import partial, reduce
from typing import Literal, cast

import numpy as np
import scipy.linalg as scipy_linalg
from numpy.exceptions import ComplexWarning
from scipy.linalg import get_lapack_funcs

import pytensor
from pytensor import ifelse
from pytensor import tensor as pt
from pytensor.gradient import DisconnectedType
from pytensor.graph.basic import Apply
from pytensor.graph.op import Op
from pytensor.raise_op import Assert
from pytensor.tensor import TensorLike
from pytensor.tensor import basic as ptb
from pytensor.tensor import math as ptm
from pytensor.tensor.basic import as_tensor_variable, diagonal
from pytensor.tensor.blockwise import Blockwise
from pytensor.tensor.nlinalg import kron, matrix_dot
from pytensor.tensor.shape import reshape
from pytensor.tensor.type import matrix, tensor, vector
from pytensor.tensor.variable import TensorVariable


logger = logging.getLogger(__name__)


class Cholesky(Op):
    # TODO: LAPACK wrapper with in-place behavior, for solve also

    __props__ = ("lower", "check_finite", "on_error", "overwrite_a")
    gufunc_signature = "(m,m)->(m,m)"

    def __init__(
        self,
        *,
        lower: bool = True,
        check_finite: bool = False,
        on_error: Literal["raise", "nan"] = "raise",
        overwrite_a: bool = False,
    ):
        self.lower = lower
        self.check_finite = check_finite
        if on_error not in ("raise", "nan"):
            raise ValueError('on_error must be one of "raise" or ""nan"')
        self.on_error = on_error
        self.overwrite_a = overwrite_a

        if self.overwrite_a:
            self.destroy_map = {0: [0]}

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0]]

    def make_node(self, x):
        x = as_tensor_variable(x)
        if x.type.ndim != 2:
            raise TypeError(
                f"Cholesky only allowed on matrix (2-D) inputs, got {x.type.ndim}-D input"
            )
        # Call scipy to find output dtype
        dtype = scipy_linalg.cholesky(np.eye(1, dtype=x.type.dtype)).dtype
        return Apply(self, [x], [tensor(shape=x.type.shape, dtype=dtype)])

    def perform(self, node, inputs, outputs):
        [x] = inputs
        [out] = outputs

        (potrf,) = scipy_linalg.get_lapack_funcs(("potrf",), (x,))

        # Quick return for square empty array
        if x.size == 0:
            out[0] = np.empty_like(x, dtype=potrf.dtype)
            return

        if self.check_finite and not np.isfinite(x).all():
            if self.on_error == "nan":
                out[0] = np.full(x.shape, np.nan, dtype=potrf.dtype)
                return
            else:
                raise ValueError("array must not contain infs or NaNs")

        # Squareness check
        if x.shape[0] != x.shape[1]:
            raise ValueError(
                "Input array is expected to be square but has " f"the shape: {x.shape}."
            )

        # Scipy cholesky only makes use of overwrite_a when it is F_CONTIGUOUS
        # If we have a `C_CONTIGUOUS` array we transpose to benefit from it
        c_contiguous_input = self.overwrite_a and x.flags["C_CONTIGUOUS"]
        if c_contiguous_input:
            x = x.T
            lower = not self.lower
            overwrite_a = True
        else:
            lower = self.lower
            overwrite_a = self.overwrite_a

        c, info = potrf(x, lower=lower, overwrite_a=overwrite_a, clean=True)

        if info != 0:
            if self.on_error == "nan":
                out[0] = np.full(x.shape, np.nan, dtype=node.outputs[0].type.dtype)
            elif info > 0:
                raise scipy_linalg.LinAlgError(
                    f"{info}-th leading minor of the array is not positive definite"
                )
            elif info < 0:
                raise ValueError(
                    f"LAPACK reported an illegal value in {-info}-th argument "
                    f'on entry to "POTRF".'
                )
        else:
            # Transpose result if input was transposed
            out[0] = c.T if c_contiguous_input else c

    def L_op(self, inputs, outputs, gradients):
        """
        Cholesky decomposition reverse-mode gradient update.

        Symbolic expression for reverse-mode Cholesky gradient taken from [#]_

        References
        ----------
        .. [#] I. Murray, "Differentiation of the Cholesky decomposition",
           http://arxiv.org/abs/1602.07527

        """

        dz = gradients[0]
        chol_x = outputs[0]

        # Replace the cholesky decomposition with 1 if there are nans
        # or solve_upper_triangular will throw a ValueError.
        if self.on_error == "nan":
            ok = ~ptm.any(ptm.isnan(chol_x))
            chol_x = ptb.switch(ok, chol_x, 1)
            dz = ptb.switch(ok, dz, 1)

        # deal with upper triangular by converting to lower triangular
        if not self.lower:
            chol_x = chol_x.T
            dz = dz.T

        def tril_and_halve_diagonal(mtx):
            """Extracts lower triangle of square matrix and halves diagonal."""
            return ptb.tril(mtx) - ptb.diag(ptb.diagonal(mtx) / 2.0)

        def conjugate_solve_triangular(outer, inner):
            """Computes L^{-T} P L^{-1} for lower-triangular L."""
            solve_upper = SolveTriangular(lower=False, b_ndim=2)
            return solve_upper(outer.T, solve_upper(outer.T, inner.T).T)

        s = conjugate_solve_triangular(
            chol_x, tril_and_halve_diagonal(chol_x.T.dot(dz))
        )

        if self.lower:
            grad = ptb.tril(s + s.T) - ptb.diag(ptb.diagonal(s))
        else:
            grad = ptb.triu(s + s.T) - ptb.diag(ptb.diagonal(s))

        if self.on_error == "nan":
            return [ptb.switch(ok, grad, np.nan)]
        else:
            return [grad]

    def inplace_on_inputs(self, allowed_inplace_inputs: list[int]) -> "Op":
        if not allowed_inplace_inputs:
            return self
        new_props = self._props_dict()  # type: ignore
        new_props["overwrite_a"] = True
        return type(self)(**new_props)


def cholesky(
    x: "TensorLike",
    lower: bool = True,
    *,
    check_finite: bool = False,
    overwrite_a: bool = False,
    on_error: Literal["raise", "nan"] = "raise",
):
    """
    Return a triangular matrix square root of positive semi-definite `x`.

    L = cholesky(X, lower=True) implies dot(L, L.T) == X.

    Parameters
    ----------
    x: tensor_like
    lower : bool, default=True
        Whether to return the lower or upper cholesky factor
    check_finite : bool, default=False
        Whether to check that the input matrix contains only finite numbers.
    overwrite_a: bool, ignored
        Whether to use the same memory for the output as `a`. This argument is ignored, and is present here only
        for consistency with scipy.linalg.cholesky.
    on_error : ['raise', 'nan']
        If on_error is set to 'raise', this Op will raise a `scipy.linalg.LinAlgError` if the matrix is not positive definite.
        If on_error is set to 'nan', it will return a matrix containing nans instead.

    Returns
    -------
    TensorVariable
        Lower or upper triangular Cholesky factor of `x`

    Example
    -------
    .. testcode::

        import pytensor
        import pytensor.tensor as pt
        import numpy as np

        x = pt.tensor('x', shape=(5, 5), dtype='float64')
        L = pt.linalg.cholesky(x)

        f = pytensor.function([x], L)
        x_value = np.random.normal(size=(5, 5))
        x_value = x_value @ x_value.T # Ensures x is positive definite
        L_value = f(x_value)
        assert np.allclose(L_value @ L_value.T, x_value)

    """

    return Blockwise(
        Cholesky(lower=lower, on_error=on_error, check_finite=check_finite)
    )(x)


class SolveBase(Op):
    """Base class for `scipy.linalg` matrix equation solvers."""

    __props__: tuple[str, ...] = (
        "lower",
        "check_finite",
        "b_ndim",
        "overwrite_a",
        "overwrite_b",
    )

    def __init__(
        self,
        *,
        lower=False,
        check_finite=True,
        b_ndim,
        overwrite_a=False,
        overwrite_b=False,
    ):
        self.lower = lower
        self.check_finite = check_finite

        assert b_ndim in (1, 2)
        self.b_ndim = b_ndim
        if b_ndim == 1:
            self.gufunc_signature = "(m,m),(m)->(m)"
        else:
            self.gufunc_signature = "(m,m),(m,n)->(m,n)"
        self.overwrite_a = overwrite_a
        self.overwrite_b = overwrite_b
        destroy_map = {}
        if self.overwrite_a and self.overwrite_b:
            # An output destroying two inputs is not yet supported
            # destroy_map[0] = [0, 1]
            raise NotImplementedError(
                "It's not yet possible to overwrite_a and overwrite_b simultaneously"
            )
        elif self.overwrite_a:
            destroy_map[0] = [0]
        elif self.overwrite_b:
            destroy_map[0] = [1]
        self.destroy_map = destroy_map

    def perform(self, node, inputs, outputs):
        raise NotImplementedError(
            "SolveBase should be subclassed with an perform method"
        )

    def make_node(self, A, b):
        A = as_tensor_variable(A)
        b = as_tensor_variable(b)

        if A.ndim != 2:
            raise ValueError(f"`A` must be a matrix; got {A.type} instead.")
        if b.ndim != self.b_ndim:
            raise ValueError(f"`b` must have {self.b_ndim} dims; got {b.type} instead.")

        # Infer dtype by solving the most simple case with 1x1 matrices
        o_dtype = scipy_linalg.solve(
            np.ones((1, 1), dtype=A.dtype),
            np.ones((1,), dtype=b.dtype),
        ).dtype
        x = tensor(dtype=o_dtype, shape=b.type.shape)
        return Apply(self, [A, b], [x])

    def infer_shape(self, fgraph, node, shapes):
        Ashape, Bshape = shapes
        rows = Ashape[1]
        if len(Bshape) == 1:
            return [(rows,)]
        else:
            cols = Bshape[1]
            return [(rows, cols)]

    def L_op(self, inputs, outputs, output_gradients):
        r"""Reverse-mode gradient updates for matrix solve operation :math:`c = A^{-1} b`.

        Symbolic expression for updates taken from [#]_.

        References
        ----------
        .. [#] M. B. Giles, "An extended collection of matrix derivative results
          for forward and reverse mode automatic differentiation",
          http://eprints.maths.ox.ac.uk/1079/

        """
        A, b = inputs

        c = outputs[0]
        # C is a scalar representing the entire graph
        # `output_gradients` is (dC/dc,)
        # We need to return (dC/d[inv(A)], dC/db)
        c_bar = output_gradients[0]

        props_dict = self._props_dict()
        props_dict["lower"] = not self.lower

        solve_op = type(self)(**props_dict)

        b_bar = solve_op(A.mT, c_bar)
        # force outer product if vector second input
        A_bar = -ptm.outer(b_bar, c) if c.ndim == 1 else -b_bar.dot(c.T)

        if props_dict.get("unit_diagonal", False):
            n = A_bar.shape[-1]
            A_bar = A_bar[pt.arange(n), pt.arange(n)].set(pt.zeros(n))

        return [A_bar, b_bar]


def _default_b_ndim(b, b_ndim):
    if b_ndim is not None:
        assert b_ndim in (1, 2)
        return b_ndim

    b = as_tensor_variable(b)
    if b_ndim is None:
        return min(b.ndim, 2)  # By default, assume the core case is a matrix


class CholeskySolve(SolveBase):
    __props__ = (
        "lower",
        "check_finite",
        "b_ndim",
        "overwrite_b",
    )

    def __init__(self, **kwargs):
        if kwargs.get("overwrite_a", False):
            raise ValueError("overwrite_a is not supported for CholeskySolve")
        kwargs.setdefault("lower", True)
        super().__init__(**kwargs)

    def make_node(self, *inputs):
        # Allow base class to do input validation
        super_apply = super().make_node(*inputs)
        A, b = super_apply.inputs
        [super_out] = super_apply.outputs
        # The dtype of chol_solve does not match solve, which the base class checks
        dtype = scipy_linalg.cho_solve(
            (np.ones((1, 1), dtype=A.dtype), False),
            np.ones((1,), dtype=b.dtype),
        ).dtype
        out = tensor(dtype=dtype, shape=super_out.type.shape)
        return Apply(self, [A, b], [out])

    def perform(self, node, inputs, output_storage):
        C, b = inputs
        rval = scipy_linalg.cho_solve(
            (C, self.lower),
            b,
            check_finite=self.check_finite,
            overwrite_b=self.overwrite_b,
        )

        output_storage[0][0] = rval

    def L_op(self, *args, **kwargs):
        # TODO: Base impl should work, let's try it
        raise NotImplementedError()

    def inplace_on_inputs(self, allowed_inplace_inputs: list[int]) -> "Op":
        if 1 in allowed_inplace_inputs:
            new_props = self._props_dict()  # type: ignore
            new_props["overwrite_b"] = True
            return type(self)(**new_props)
        else:
            return self


def cho_solve(
    c_and_lower: tuple[TensorLike, bool],
    b: TensorLike,
    *,
    check_finite: bool = True,
    b_ndim: int | None = None,
):
    """Solve the linear equations A x = b, given the Cholesky factorization of A.

    Parameters
    ----------
    c_and_lower : tuple of (TensorLike, bool)
        Cholesky factorization of a, as given by cho_factor
    b : TensorLike
        Right-hand side
    check_finite : bool, optional
        Whether to check that the input matrices contain only finite numbers.
        Disabling may give a performance gain, but may result in problems
        (crashes, non-termination) if the inputs do contain infinities or NaNs.
    b_ndim : int
        Whether the core case of b is a vector (1) or matrix (2).
        This will influence how batched dimensions are interpreted.
    """
    A, lower = c_and_lower
    b_ndim = _default_b_ndim(b, b_ndim)
    return Blockwise(
        CholeskySolve(lower=lower, check_finite=check_finite, b_ndim=b_ndim)
    )(A, b)


class LU(Op):
    """Decompose a matrix into lower and upper triangular matrices."""

    __props__ = ("permute_l", "overwrite_a", "check_finite", "p_indices")

    def __init__(
        self, *, permute_l=False, overwrite_a=False, check_finite=True, p_indices=False
    ):
        if permute_l and p_indices:
            raise ValueError("Only one of permute_l and p_indices can be True")
        self.permute_l = permute_l
        self.check_finite = check_finite
        self.p_indices = p_indices
        self.overwrite_a = overwrite_a

        if self.permute_l:
            # permute_l overrides p_indices in the scipy function. We can copy that behavior
            self.gufunc_signature = "(m,m)->(m,m),(m,m)"
        elif self.p_indices:
            self.gufunc_signature = "(m,m)->(m),(m,m),(m,m)"
        else:
            self.gufunc_signature = "(m,m)->(m,m),(m,m),(m,m)"

        if self.overwrite_a:
            self.destroy_map = {0: [0]} if self.permute_l else {1: [0]}

    def infer_shape(self, fgraph, node, shapes):
        n = shapes[0][0]
        if self.permute_l:
            return [(n, n), (n, n)]
        elif self.p_indices:
            return [(n,), (n, n), (n, n)]
        else:
            return [(n, n), (n, n), (n, n)]

    def make_node(self, x):
        x = as_tensor_variable(x)
        if x.type.ndim != 2:
            raise TypeError(
                f"LU only allowed on matrix (2-D) inputs, got {x.type.ndim}-D input"
            )

        real_dtype = "f" if np.dtype(x.type.dtype).char in "fF" else "d"
        p_dtype = "int32" if self.p_indices else np.dtype(real_dtype)

        L = tensor(shape=x.type.shape, dtype=x.type.dtype)
        U = tensor(shape=x.type.shape, dtype=x.type.dtype)

        if self.permute_l:
            # In this case, L is actually P @ L
            return Apply(self, inputs=[x], outputs=[L, U])
        if self.p_indices:
            p_indices = tensor(shape=(x.type.shape[0],), dtype=p_dtype)
            return Apply(self, inputs=[x], outputs=[p_indices, L, U])

        P = tensor(shape=x.type.shape, dtype=p_dtype)
        return Apply(self, inputs=[x], outputs=[P, L, U])

    def perform(self, node, inputs, outputs):
        [A] = inputs

        out = scipy_linalg.lu(
            A,
            permute_l=self.permute_l,
            overwrite_a=self.overwrite_a,
            check_finite=self.check_finite,
            p_indices=self.p_indices,
        )

        outputs[0][0] = out[0]
        outputs[1][0] = out[1]

        if not self.permute_l:
            # In all cases except permute_l, there are three returns
            outputs[2][0] = out[2]

    def inplace_on_inputs(self, allowed_inplace_inputs: list[int]) -> "Op":
        if 0 in allowed_inplace_inputs:
            new_props = self._props_dict()  # type: ignore
            new_props["overwrite_a"] = True
            return type(self)(**new_props)

        else:
            return self

    def L_op(
        self,
        inputs: Sequence[ptb.Variable],
        outputs: Sequence[ptb.Variable],
        output_grads: Sequence[ptb.Variable],
    ) -> list[ptb.Variable]:
        r"""
        Derivation is due to Differentiation of Matrix Functionals Using Triangular Factorization
        F. R. De Hoog, R.S. Anderssen, M. A. Lukas
        """
        [A] = inputs
        A = cast(TensorVariable, A)

        if self.permute_l:
            # P has no gradient contribution (by assumption...), so PL_bar is the same as L_bar
            L_bar, U_bar = output_grads

            # TODO: Rewrite into permute_l = False for graphs where we need to compute the gradient
            # We need L, not PL. It's not possible to recover it from PL, though. So we need to do a new forward pass
            P_or_indices, L, U = lu(  # type: ignore
                A, permute_l=False, check_finite=self.check_finite, p_indices=False
            )

        else:
            # In both other cases, there are 3 outputs. The first output will either be the permutation index itself,
            # or indices that can be used to reconstruct the permutation matrix.
            P_or_indices, L, U = outputs
            _, L_bar, U_bar = output_grads

        L_bar = (
            L_bar if not isinstance(L_bar.type, DisconnectedType) else pt.zeros_like(A)
        )
        U_bar = (
            U_bar if not isinstance(U_bar.type, DisconnectedType) else pt.zeros_like(A)
        )

        x1 = ptb.tril(L.T @ L_bar, k=-1)
        x2 = ptb.triu(U_bar @ U.T)

        LT_inv_x = solve_triangular(L.T, x1 + x2, lower=False, unit_diagonal=True)

        # Where B = P.T @ A is a change of variable to avoid the permutation matrix in the gradient derivation
        B_bar = solve_triangular(U, LT_inv_x.T, lower=False).T

        if not self.p_indices:
            A_bar = P_or_indices @ B_bar
        else:
            A_bar = B_bar[P_or_indices]

        return [A_bar]


def lu(
    a: TensorLike,
    permute_l=False,
    check_finite=True,
    p_indices=False,
    overwrite_a: bool = False,
) -> (
    tuple[TensorVariable, TensorVariable, TensorVariable]
    | tuple[TensorVariable, TensorVariable]
):
    """
    Factorize a matrix as the product of a unit lower triangular matrix and an upper triangular matrix:

    ... math::

        A = P L U

    Where P is a permutation matrix, L is lower triangular with unit diagonal elements, and U is upper triangular.

    Parameters
    ----------
    a: TensorLike
        Matrix to be factorized
    permute_l: bool
        If True, L is a product of permutation and unit lower triangular matrices. Only two values, PL and U, will
        be returned in this case, and PL will not be lower triangular.
    check_finite: bool
        Whether to check that the input matrix contains only finite numbers.
    p_indices: bool
        If True, return integer matrix indices for the permutation matrix. Otherwise, return the permutation matrix
        itself.
    overwrite_a: bool
        Ignored by Pytensor. Pytensor will always perform computation inplace if possible.
    Returns
    -------
    P: TensorVariable
        Permutation matrix, or array of integer indices for permutation matrix. Not returned if permute_l is True.
    L: TensorVariable
        Lower triangular matrix, or product of permutation and unit lower triangular matrices if permute_l is True.
    U: TensorVariable
        Upper triangular matrix
    """
    return cast(
        tuple[TensorVariable, TensorVariable, TensorVariable]
        | tuple[TensorVariable, TensorVariable],
        Blockwise(
            LU(permute_l=permute_l, p_indices=p_indices, check_finite=check_finite)
        )(a),
    )


class PivotToPermutations(Op):
    gufunc_signature = "(x)->(x)"
    __props__ = ("inverse",)

    def __init__(self, inverse=True):
        self.inverse = inverse

    def make_node(self, pivots):
        pivots = as_tensor_variable(pivots)
        if pivots.ndim != 1:
            raise ValueError("PivotToPermutations only works on 1-D inputs")

        permutations = pivots.type.clone(dtype="int64")()
        return Apply(self, [pivots], [permutations])

    def perform(self, node, inputs, outputs):
        [pivots] = inputs
        p_inv = np.arange(len(pivots), dtype="int64")

        for i in range(len(pivots)):
            p_inv[i], p_inv[pivots[i]] = p_inv[pivots[i]], p_inv[i]

        if self.inverse:
            outputs[0][0] = p_inv
        else:
            outputs[0][0] = np.argsort(p_inv)


def pivot_to_permutation(p: TensorLike, inverse=False):
    p = pt.as_tensor_variable(p)
    return PivotToPermutations(inverse=inverse)(p)


class LUFactor(Op):
    __props__ = ("overwrite_a", "check_finite")
    gufunc_signature = "(m,m)->(m,m),(m)"

    def __init__(self, *, overwrite_a=False, check_finite=True):
        self.overwrite_a = overwrite_a
        self.check_finite = check_finite

        if self.overwrite_a:
            self.destroy_map = {1: [0]}

    def make_node(self, A):
        A = as_tensor_variable(A)
        if A.type.ndim != 2:
            raise TypeError(
                f"LU only allowed on matrix (2-D) inputs, got {A.type.ndim}-D input"
            )

        LU = matrix(shape=A.type.shape, dtype=A.type.dtype)
        pivots = vector(shape=(A.type.shape[0],), dtype="int32")

        return Apply(self, [A], [LU, pivots])

    def infer_shape(self, fgraph, node, shapes):
        n = shapes[0][0]
        return [(n, n), (n,)]

    def inplace_on_inputs(self, allowed_inplace_inputs: list[int]) -> "Op":
        if 0 in allowed_inplace_inputs:
            new_props = self._props_dict()  # type: ignore
            new_props["overwrite_a"] = True
            return type(self)(**new_props)
        else:
            return self

    def perform(self, node, inputs, outputs):
        A = inputs[0]

        LU, p = scipy_linalg.lu_factor(
            A, overwrite_a=self.overwrite_a, check_finite=self.check_finite
        )

        outputs[0][0] = LU
        outputs[1][0] = p

    def L_op(self, inputs, outputs, output_gradients):
        [A] = inputs
        LU_bar, _ = output_gradients
        LU, p_indices = outputs

        eye = ptb.identity_like(A)
        L = cast(TensorVariable, ptb.tril(LU, k=-1) + eye)
        U = cast(TensorVariable, ptb.triu(LU))

        p_indices = pivot_to_permutation(p_indices, inverse=False)

        # Split LU_bar into L_bar and U_bar. This is valid because of the triangular structure of L and U
        L_bar = ptb.tril(LU_bar, k=-1)
        U_bar = ptb.triu(LU_bar)

        # From here we're in the same situation as the LU gradient derivation
        x1 = ptb.tril(L.T @ L_bar, k=-1)
        x2 = ptb.triu(U_bar @ U.T)

        LT_inv_x = solve_triangular(L.T, x1 + x2, lower=False, unit_diagonal=True)
        B_bar = solve_triangular(U, LT_inv_x.T, lower=False).T
        A_bar = B_bar[p_indices]

        return [A_bar]


def lu_factor(
    a: TensorLike,
    *,
    check_finite: bool = True,
    overwrite_a: bool = False,
) -> tuple[TensorVariable, TensorVariable]:
    """
    LU factorization with partial pivoting.

    Parameters
    ----------
    a: TensorLike
        Matrix to be factorized
    check_finite: bool
        Whether to check that the input matrix contains only finite numbers.
    overwrite_a: bool
        Unused by PyTensor. PyTensor will always perform the operation in-place if possible.

    Returns
    -------
    LU: TensorVariable
        LU decomposition of `a`
    pivots: TensorVariable
        An array of integers representin the pivot indices
    """

    return cast(
        tuple[TensorVariable, TensorVariable],
        Blockwise(LUFactor(check_finite=check_finite))(a),
    )


def _lu_solve(
    LU: TensorLike,
    pivots: TensorLike,
    b: TensorLike,
    trans: bool = False,
    b_ndim: int | None = None,
    check_finite: bool = True,
):
    b_ndim = _default_b_ndim(b, b_ndim)

    LU, pivots, b = map(pt.as_tensor_variable, [LU, pivots, b])

    inv_permutation = pivot_to_permutation(pivots, inverse=True)
    x = b[inv_permutation] if not trans else b
    # TODO: Use PermuteRows on b
    # x = permute_rows(b, pivots) if not trans else b

    x = solve_triangular(
        LU,
        x,
        lower=not trans,
        unit_diagonal=not trans,
        trans=trans,
        b_ndim=b_ndim,
        check_finite=check_finite,
    )

    x = solve_triangular(
        LU,
        x,
        lower=trans,
        unit_diagonal=trans,
        trans=trans,
        b_ndim=b_ndim,
        check_finite=check_finite,
    )

    # TODO: Use PermuteRows(inverse=True) on x
    # if trans:
    #     x = permute_rows(x, pivots, inverse=True)
    x = x[pt.argsort(inv_permutation)] if trans else x
    return x


def lu_solve(
    LU_and_pivots: tuple[TensorLike, TensorLike],
    b: TensorLike,
    trans: bool = False,
    b_ndim: int | None = None,
    check_finite: bool = True,
    overwrite_b: bool = False,
):
    """
    Solve a system of linear equations given the LU decomposition of the matrix.

    Parameters
    ----------
    LU_and_pivots: tuple[TensorLike, TensorLike]
        LU decomposition of the matrix, as returned by `lu_factor`
    b: TensorLike
        Right-hand side of the equation
    trans: bool
        If True, solve A^T x = b, instead of Ax = b. Default is False
    b_ndim: int, optional
        The number of core dimensions in b. Used to distinguish between a batch of vectors (b_ndim=1) and a matrix
        of vectors (b_ndim=2). Default is None, which will infer the number of core dimensions from the input.
    check_finite: bool
        If True, check that the input matrices contain only finite numbers. Default is True.
    overwrite_b: bool
        Ignored by Pytensor. Pytensor will always compute inplace when possible.
    """
    b_ndim = _default_b_ndim(b, b_ndim)
    if b_ndim == 1:
        signature = "(m,m),(m),(m)->(m)"
    else:
        signature = "(m,m),(m),(m,n)->(m,n)"
    partialled_func = partial(
        _lu_solve, trans=trans, b_ndim=b_ndim, check_finite=check_finite
    )
    return pt.vectorize(partialled_func, signature=signature)(*LU_and_pivots, b)


class SolveTriangular(SolveBase):
    """Solve a system of linear equations."""

    __props__ = (
        "unit_diagonal",
        "lower",
        "check_finite",
        "b_ndim",
        "overwrite_b",
    )

    def __init__(self, *, unit_diagonal=False, **kwargs):
        if kwargs.get("overwrite_a", False):
            raise ValueError("overwrite_a is not supported for SolverTriangulare")

        # There's a naming inconsistency between solve_triangular (trans) and solve (transposed). Internally, we can use
        # transpose everywhere, but expose the same API as scipy.linalg.solve_triangular
        super().__init__(**kwargs)
        self.unit_diagonal = unit_diagonal

    def perform(self, node, inputs, outputs):
        A, b = inputs
        outputs[0][0] = scipy_linalg.solve_triangular(
            A,
            b,
            lower=self.lower,
            trans=0,
            unit_diagonal=self.unit_diagonal,
            check_finite=self.check_finite,
            overwrite_b=self.overwrite_b,
        )

    def L_op(self, inputs, outputs, output_gradients):
        res = super().L_op(inputs, outputs, output_gradients)

        if self.lower:
            res[0] = ptb.tril(res[0])
        else:
            res[0] = ptb.triu(res[0])

        return res

    def inplace_on_inputs(self, allowed_inplace_inputs: list[int]) -> "Op":
        if 1 in allowed_inplace_inputs:
            new_props = self._props_dict()  # type: ignore
            new_props["overwrite_b"] = True
            return type(self)(**new_props)
        else:
            return self


def solve_triangular(
    a: TensorVariable,
    b: TensorVariable,
    *,
    trans: int | str = 0,
    lower: bool = False,
    unit_diagonal: bool = False,
    check_finite: bool = True,
    b_ndim: int | None = None,
) -> TensorVariable:
    """Solve the equation `a x = b` for `x`, assuming `a` is a triangular matrix.

    Parameters
    ----------
    a: TensorVariable
        Square input data
    b: TensorVariable
        Input data for the right hand side.
    lower : bool, optional
        Use only data contained in the lower triangle of `a`. Default is to use upper triangle.
    trans: {0, 1, 2, 'N', 'T', 'C'}, optional
        Type of system to solve:
        trans       system
        0 or 'N'    a x = b
        1 or 'T'    a^T x = b
        2 or 'C'    a^H x = b
    unit_diagonal: bool, optional
        If True, diagonal elements of `a` are assumed to be 1 and will not be referenced.
    check_finite : bool, optional
        Whether to check that the input matrices contain only finite numbers.
        Disabling may give a performance gain, but may result in problems
        (crashes, non-termination) if the inputs do contain infinities or NaNs.
    b_ndim : int
        Whether the core case of b is a vector (1) or matrix (2).
        This will influence how batched dimensions are interpreted.
    """
    b_ndim = _default_b_ndim(b, b_ndim)

    if trans in [1, "T", True]:
        a = a.mT
        lower = not lower
    if trans in [2, "C"]:
        a = a.conj().mT
        lower = not lower

    ret = Blockwise(
        SolveTriangular(
            lower=lower,
            unit_diagonal=unit_diagonal,
            check_finite=check_finite,
            b_ndim=b_ndim,
        )
    )(a, b)
    return cast(TensorVariable, ret)


class Solve(SolveBase):
    """
    Solve a system of linear equations.
    """

    __props__ = (
        "assume_a",
        "lower",
        "check_finite",
        "b_ndim",
        "overwrite_a",
        "overwrite_b",
    )

    def __init__(self, *, assume_a="gen", **kwargs):
        # Triangular and diagonal are handled outside of Solve
        valid_options = ["gen", "sym", "her", "pos", "tridiagonal", "banded"]

        assume_a = assume_a.lower()
        # We use the old names as the different dispatches are more likely to support them
        long_to_short = {
            "general": "gen",
            "symmetric": "sym",
            "hermitian": "her",
            "positive definite": "pos",
        }
        assume_a = long_to_short.get(assume_a, assume_a)

        if assume_a not in valid_options:
            raise ValueError(
                f"Invalid assume_a: {assume_a}. It must be one of {valid_options} or {list(long_to_short.keys())}"
            )

        if assume_a in ("tridiagonal", "banded"):
            from scipy import __version__ as sp_version

            if tuple(map(int, sp_version.split(".")[:-1])) < (1, 15):
                warnings.warn(
                    f"assume_a={assume_a} requires scipy>=1.5.0. Defaulting to assume_a='gen'.",
                    UserWarning,
                )
                assume_a = "gen"

        super().__init__(**kwargs)
        self.assume_a = assume_a

    def perform(self, node, inputs, outputs):
        a, b = inputs
        outputs[0][0] = scipy_linalg.solve(
            a=a,
            b=b,
            lower=self.lower,
            check_finite=self.check_finite,
            assume_a=self.assume_a,
            overwrite_a=self.overwrite_a,
            overwrite_b=self.overwrite_b,
        )

    def inplace_on_inputs(self, allowed_inplace_inputs: list[int]) -> "Op":
        if not allowed_inplace_inputs:
            return self
        new_props = self._props_dict()  # type: ignore
        # PyTensor doesn't allow an output to destroy two inputs yet
        # new_props["overwrite_a"] = 0 in allowed_inplace_inputs
        # new_props["overwrite_b"] = 1 in allowed_inplace_inputs
        if 1 in allowed_inplace_inputs:
            # Give preference to overwrite_b
            new_props["overwrite_b"] = True
        # We can't overwrite_a if we're assuming tridiagonal
        elif not self.assume_a == "tridiagonal":  # allowed inputs == [0]
            new_props["overwrite_a"] = True
        return type(self)(**new_props)


def solve(
    a,
    b,
    *,
    lower: bool = False,
    overwrite_a: bool = False,
    overwrite_b: bool = False,
    check_finite: bool = True,
    assume_a: str = "gen",
    transposed: bool = False,
    b_ndim: int | None = None,
):
    """Solves the linear equation set ``a * x = b`` for the unknown ``x`` for square ``a`` matrix.

    If the data matrix is known to be a particular type then supplying the
    corresponding string to ``assume_a`` key chooses the dedicated solver.
    The available options are

    ===================  ================================
     diagonal             'diagonal'
     tridiagonal          'tridiagonal'
     banded               'banded'
     upper triangular     'upper triangular'
     lower triangular     'lower triangular'
     symmetric            'symmetric' (or 'sym')
     hermitian            'hermitian' (or 'her')
     positive definite    'positive definite' (or 'pos')
     general              'general' (or 'gen')
    ===================  ================================

    If omitted, ``'general'`` is the default structure.

    The datatype of the arrays define which solver is called regardless
    of the values. In other words, even when the complex array entries have
    precisely zero imaginary parts, the complex solver will be called based
    on the data type of the array.

    Parameters
    ----------
    a : (..., N, N) array_like
        Square input data
    b : (..., N, NRHS) array_like
        Input data for the right hand side.
    lower : bool, default False
        Ignored unless ``assume_a`` is one of ``'sym'``, ``'her'``, or ``'pos'``.
        If True, the calculation uses only the data in the lower triangle of `a`;
        entries above the diagonal are ignored. If False (default), the
        calculation uses only the data in the upper triangle of `a`; entries
        below the diagonal are ignored.
    overwrite_a : bool
        Unused by PyTensor. PyTensor will always perform the operation in-place if possible.
    overwrite_b : bool
        Unused by PyTensor. PyTensor will always perform the operation in-place if possible.
    check_finite : bool, optional
        Whether to check that the input matrices contain only finite numbers.
        Disabling may give a performance gain, but may result in problems
        (crashes, non-termination) if the inputs do contain infinities or NaNs.
    assume_a : str, optional
        Valid entries are explained above.
    transposed: bool, default False
        If True, solves the system A^T x = b. Default is False.
    b_ndim : int
        Whether the core case of b is a vector (1) or matrix (2).
        This will influence how batched dimensions are interpreted.
        By default, we assume b_ndim = b.ndim is 2 if b.ndim > 1, else 1.
    """
    assume_a = assume_a.lower()

    if assume_a in ("lower triangular", "upper triangular"):
        lower = "lower" in assume_a
        return solve_triangular(
            a,
            b,
            lower=lower,
            trans=transposed,
            check_finite=check_finite,
            b_ndim=b_ndim,
        )

    b_ndim = _default_b_ndim(b, b_ndim)

    if assume_a == "diagonal":
        a_diagonal = diagonal(a, axis1=-2, axis2=-1)
        b_transposed = b[None, :] if b_ndim == 1 else b.mT
        x = (b_transposed / pt.expand_dims(a_diagonal, -2)).mT
        if b_ndim == 1:
            x = x.squeeze(-1)
        return x

    if transposed:
        a = a.mT
        lower = not lower

    return Blockwise(
        Solve(
            lower=lower,
            check_finite=check_finite,
            assume_a=assume_a,
            b_ndim=b_ndim,
        )
    )(a, b)


class Eigvalsh(Op):
    """
    Generalized eigenvalues of a Hermitian positive definite eigensystem.

    """

    __props__ = ("lower",)

    def __init__(self, lower=True):
        assert lower in [True, False]
        self.lower = lower

    def make_node(self, a, b):
        if b == pytensor.tensor.type_other.NoneConst:
            a = as_tensor_variable(a)
            assert a.ndim == 2

            out_dtype = pytensor.scalar.upcast(a.dtype)
            w = vector(dtype=out_dtype)
            return Apply(self, [a], [w])
        else:
            a = as_tensor_variable(a)
            b = as_tensor_variable(b)
            assert a.ndim == 2
            assert b.ndim == 2

            out_dtype = pytensor.scalar.upcast(a.dtype, b.dtype)
            w = vector(dtype=out_dtype)
            return Apply(self, [a, b], [w])

    def perform(self, node, inputs, outputs):
        (w,) = outputs
        if len(inputs) == 2:
            w[0] = scipy_linalg.eigvalsh(a=inputs[0], b=inputs[1], lower=self.lower)
        else:
            w[0] = scipy_linalg.eigvalsh(a=inputs[0], b=None, lower=self.lower)

    def grad(self, inputs, g_outputs):
        a, b = inputs
        (gw,) = g_outputs
        return EigvalshGrad(self.lower)(a, b, gw)

    def infer_shape(self, fgraph, node, shapes):
        n = shapes[0][0]
        return [(n,)]


class EigvalshGrad(Op):
    """
    Gradient of generalized eigenvalues of a Hermitian positive definite
    eigensystem.

    """

    # Note: This Op (EigvalshGrad), should be removed and replaced with a graph
    # of pytensor ops that is constructed directly in Eigvalsh.grad.
    # But this can only be done once scipy.linalg.eigh is available as an Op
    # (currently the Eigh uses numpy.linalg.eigh, which doesn't let you
    # pass the right-hand-side matrix for a generalized eigenproblem.) See the
    # discussion on GitHub at
    # https://github.com/Theano/Theano/pull/1846#discussion-diff-12486764

    __props__ = ("lower",)

    def __init__(self, lower=True):
        assert lower in [True, False]
        self.lower = lower
        if lower:
            self.tri0 = np.tril
            self.tri1 = lambda a: np.triu(a, 1)
        else:
            self.tri0 = np.triu
            self.tri1 = lambda a: np.tril(a, -1)

    def make_node(self, a, b, gw):
        a = as_tensor_variable(a)
        b = as_tensor_variable(b)
        gw = as_tensor_variable(gw)
        assert a.ndim == 2
        assert b.ndim == 2
        assert gw.ndim == 1

        out_dtype = pytensor.scalar.upcast(a.dtype, b.dtype, gw.dtype)
        out1 = matrix(dtype=out_dtype)
        out2 = matrix(dtype=out_dtype)
        return Apply(self, [a, b, gw], [out1, out2])

    def perform(self, node, inputs, outputs):
        (a, b, gw) = inputs
        w, v = scipy_linalg.eigh(a, b, lower=self.lower)
        gA = v.dot(np.diag(gw).dot(v.T))
        gB = -v.dot(np.diag(gw * w).dot(v.T))

        # See EighGrad comments for an explanation of these lines
        out1 = self.tri0(gA) + self.tri1(gA).T
        out2 = self.tri0(gB) + self.tri1(gB).T
        outputs[0][0] = np.asarray(out1, dtype=node.outputs[0].dtype)
        outputs[1][0] = np.asarray(out2, dtype=node.outputs[1].dtype)

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0], shapes[1]]


def eigvalsh(a, b, lower=True):
    return Eigvalsh(lower)(a, b)


class Expm(Op):
    """
    Compute the matrix exponential of a square array.

    """

    __props__ = ()

    def make_node(self, A):
        A = as_tensor_variable(A)
        assert A.ndim == 2
        expm = matrix(dtype=A.dtype)
        return Apply(
            self,
            [
                A,
            ],
            [
                expm,
            ],
        )

    def perform(self, node, inputs, outputs):
        (A,) = inputs
        (expm,) = outputs
        expm[0] = scipy_linalg.expm(A)

    def grad(self, inputs, outputs):
        (A,) = inputs
        (g_out,) = outputs
        return [ExpmGrad()(A, g_out)]

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0]]


class ExpmGrad(Op):
    """
    Gradient of the matrix exponential of a square array.

    """

    __props__ = ()

    def make_node(self, A, gw):
        A = as_tensor_variable(A)
        assert A.ndim == 2
        out = matrix(dtype=A.dtype)
        return Apply(
            self,
            [A, gw],
            [
                out,
            ],
        )

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0]]

    def perform(self, node, inputs, outputs):
        # Kalbfleisch and Lawless, J. Am. Stat. Assoc. 80 (1985) Equation 3.4
        # Kind of... You need to do some algebra from there to arrive at
        # this expression.
        (A, gA) = inputs
        (out,) = outputs
        w, V = scipy_linalg.eig(A, right=True)
        U = scipy_linalg.inv(V).T

        exp_w = np.exp(w)
        X = np.subtract.outer(exp_w, exp_w) / np.subtract.outer(w, w)
        np.fill_diagonal(X, exp_w)
        Y = U.dot(V.T.dot(gA).dot(U) * X).dot(V.T)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ComplexWarning)
            out[0] = Y.astype(A.dtype)


expm = Expm()


class SolveContinuousLyapunov(Op):
    """
    Solves a continuous Lyapunov equation, :math:`AX + XA^H = B`, for :math:`X.

    Continuous time Lyapunov equations are special cases of Sylvester equations, :math:`AX + XB = C`, and can be solved
    efficiently using the Bartels-Stewart algorithm. For more details, see the docstring for
    scipy.linalg.solve_continuous_lyapunov
    """

    __props__ = ()
    gufunc_signature = "(m,m),(m,m)->(m,m)"

    def make_node(self, A, B):
        A = as_tensor_variable(A)
        B = as_tensor_variable(B)

        out_dtype = pytensor.scalar.upcast(A.dtype, B.dtype)
        X = pytensor.tensor.matrix(dtype=out_dtype)

        return pytensor.graph.basic.Apply(self, [A, B], [X])

    def perform(self, node, inputs, output_storage):
        (A, B) = inputs
        X = output_storage[0]

        out_dtype = node.outputs[0].type.dtype
        X[0] = scipy_linalg.solve_continuous_lyapunov(A, B).astype(out_dtype)

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0]]

    def grad(self, inputs, output_grads):
        # Gradient computations come from Kao and Hennequin (2020), https://arxiv.org/pdf/2011.11430.pdf
        # Note that they write the equation as AX + XA.H + Q = 0, while scipy uses AX + XA^H = Q,
        # so minor adjustments need to be made.
        A, Q = inputs
        (dX,) = output_grads

        X = self(A, Q)
        S = self(A.conj().T, -dX)  # Eq 31, adjusted

        A_bar = S.dot(X.conj().T) + S.conj().T.dot(X)
        Q_bar = -S  # Eq 29, adjusted

        return [A_bar, Q_bar]


_solve_continuous_lyapunov = Blockwise(SolveContinuousLyapunov())


def solve_continuous_lyapunov(A: TensorLike, Q: TensorLike) -> TensorVariable:
    """
    Solve the continuous Lyapunov equation :math:`A X + X A^H + Q = 0`.

    Parameters
    ----------
    A: TensorLike
        Square matrix of shape ``N x N``.
    Q: TensorLike
        Square matrix of shape ``N x N``.

    Returns
    -------
    X: TensorVariable
        Square matrix of shape ``N x N``

    """

    return cast(TensorVariable, _solve_continuous_lyapunov(A, Q))


class BilinearSolveDiscreteLyapunov(Op):
    """
    Solves a discrete lyapunov equation, :math:`AXA^H - X = Q`, for :math:`X.

    The solution is computed by first transforming the discrete-time problem into a continuous-time form. The continuous
    time lyapunov is a special case of a Sylvester equation, and can be efficiently solved. For more details, see the
    docstring for scipy.linalg.solve_discrete_lyapunov
    """

    gufunc_signature = "(m,m),(m,m)->(m,m)"

    def make_node(self, A, B):
        A = as_tensor_variable(A)
        B = as_tensor_variable(B)

        out_dtype = pytensor.scalar.upcast(A.dtype, B.dtype)
        X = pytensor.tensor.matrix(dtype=out_dtype)

        return pytensor.graph.basic.Apply(self, [A, B], [X])

    def perform(self, node, inputs, output_storage):
        (A, B) = inputs
        X = output_storage[0]

        out_dtype = node.outputs[0].type.dtype
        X[0] = scipy_linalg.solve_discrete_lyapunov(A, B, method="bilinear").astype(
            out_dtype
        )

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0]]

    def grad(self, inputs, output_grads):
        # Gradient computations come from Kao and Hennequin (2020), https://arxiv.org/pdf/2011.11430.pdf
        A, Q = inputs
        (dX,) = output_grads

        X = self(A, Q)

        # Eq 41, note that it is not written as a proper Lyapunov equation
        S = self(A.conj().T, dX)

        A_bar = pytensor.tensor.linalg.matrix_dot(
            S, A, X.conj().T
        ) + pytensor.tensor.linalg.matrix_dot(S.conj().T, A, X)
        Q_bar = S
        return [A_bar, Q_bar]


_bilinear_solve_discrete_lyapunov = Blockwise(BilinearSolveDiscreteLyapunov())


def _direct_solve_discrete_lyapunov(
    A: TensorVariable, Q: TensorVariable
) -> TensorVariable:
    r"""
    Directly solve the discrete Lyapunov equation :math:`A X A^H - X = Q` using the kronecker method of Magnus and
    Neudecker.

    This involves constructing and inverting an intermediate matrix :math:`A \otimes A`, with shape :math:`N^2 x N^2`.
    As a result, this method scales poorly with the size of :math:`N`, and should be avoided for large :math:`N`.
    """

    if A.type.dtype.startswith("complex"):
        AxA = kron(A, A.conj())
    else:
        AxA = kron(A, A)

    eye = pt.eye(AxA.shape[-1])

    vec_Q = Q.ravel()
    vec_X = solve(eye - AxA, vec_Q, b_ndim=1)

    return reshape(vec_X, A.shape)


def solve_discrete_lyapunov(
    A: TensorLike,
    Q: TensorLike,
    method: Literal["direct", "bilinear"] = "bilinear",
) -> TensorVariable:
    """Solve the discrete Lyapunov equation :math:`A X A^H - X = Q`.

    Parameters
    ----------
    A: TensorLike
        Square matrix of shape N x N
    Q: TensorLike
        Square matrix of shape N x N
    method: str, one of ``"direct"`` or ``"bilinear"``
        Solver method used, . ``"direct"`` solves the problem directly via matrix inversion.  This has a pure
        PyTensor implementation and can thus be cross-compiled to supported backends, and should be preferred when
         ``N`` is not large. The direct method scales poorly with the size of ``N``, and the bilinear can be
        used in these cases.

    Returns
    -------
    X: TensorVariable
        Square matrix of shape ``N x N``. Solution to the Lyapunov equation

    """
    if method not in ["direct", "bilinear"]:
        raise ValueError(
            f'Parameter "method" must be one of "direct" or "bilinear", found {method}'
        )

    A = as_tensor_variable(A)
    Q = as_tensor_variable(Q)

    if method == "direct":
        signature = BilinearSolveDiscreteLyapunov.gufunc_signature
        X = pt.vectorize(_direct_solve_discrete_lyapunov, signature=signature)(A, Q)
        return cast(TensorVariable, X)

    elif method == "bilinear":
        return cast(TensorVariable, _bilinear_solve_discrete_lyapunov(A, Q))

    else:
        raise ValueError(f"Unknown method {method}")


class SolveDiscreteARE(Op):
    __props__ = ("enforce_Q_symmetric",)
    gufunc_signature = "(m,m),(m,n),(m,m),(n,n)->(m,m)"

    def __init__(self, enforce_Q_symmetric: bool = False):
        self.enforce_Q_symmetric = enforce_Q_symmetric

    def make_node(self, A, B, Q, R):
        A = as_tensor_variable(A)
        B = as_tensor_variable(B)
        Q = as_tensor_variable(Q)
        R = as_tensor_variable(R)

        out_dtype = pytensor.scalar.upcast(A.dtype, B.dtype, Q.dtype, R.dtype)
        X = pytensor.tensor.matrix(dtype=out_dtype)

        return pytensor.graph.basic.Apply(self, [A, B, Q, R], [X])

    def perform(self, node, inputs, output_storage):
        A, B, Q, R = inputs
        X = output_storage[0]

        if self.enforce_Q_symmetric:
            Q = 0.5 * (Q + Q.T)

        out_dtype = node.outputs[0].type.dtype
        X[0] = scipy_linalg.solve_discrete_are(A, B, Q, R).astype(out_dtype)

    def infer_shape(self, fgraph, node, shapes):
        return [shapes[0]]

    def grad(self, inputs, output_grads):
        # Gradient computations come from Kao and Hennequin (2020), https://arxiv.org/pdf/2011.11430.pdf
        A, B, Q, R = inputs

        (dX,) = output_grads
        X = self(A, B, Q, R)

        K_inner = R + matrix_dot(B.T, X, B)

        # K_inner is guaranteed to be symmetric, because X and R are symmetric
        K_inner_inv_BT = solve(K_inner, B.T, assume_a="sym")
        K = matrix_dot(K_inner_inv_BT, X, A)

        A_tilde = A - B.dot(K)

        dX_symm = 0.5 * (dX + dX.T)
        S = solve_discrete_lyapunov(A_tilde, dX_symm)

        A_bar = 2 * matrix_dot(X, A_tilde, S)
        B_bar = -2 * matrix_dot(X, A_tilde, S, K.T)
        Q_bar = S
        R_bar = matrix_dot(K, S, K.T)

        return [A_bar, B_bar, Q_bar, R_bar]


def solve_discrete_are(
    A: TensorLike,
    B: TensorLike,
    Q: TensorLike,
    R: TensorLike,
    enforce_Q_symmetric: bool = False,
) -> TensorVariable:
    """
    Solve the discrete Algebraic Riccati equation :math:`A^TXA - X - (A^TXB)(R + B^TXB)^{-1}(B^TXA) + Q = 0`.

    Discrete-time Algebraic Riccati equations arise in the context of optimal control and filtering problems, as the
    solution to Linear-Quadratic Regulators (LQR), Linear-Quadratic-Guassian (LQG) control problems, and as the
    steady-state covariance of the Kalman Filter.

    Such problems typically have many solutions, but we are generally only interested in the unique *stabilizing*
    solution. This stable solution, if it exists, will be returned by this function.

    Parameters
    ----------
    A: TensorLike
        Square matrix of shape M x M
    B: TensorLike
        Square matrix of shape M x M
    Q: TensorLike
        Symmetric square matrix of shape M x M
    R: TensorLike
        Square matrix of shape N x N
    enforce_Q_symmetric: bool
        If True, the provided Q matrix is transformed to 0.5 * (Q + Q.T) to ensure symmetry

    Returns
    -------
    X: TensorVariable
        Square matrix of shape M x M, representing the solution to the DARE
    """

    return cast(
        TensorVariable, Blockwise(SolveDiscreteARE(enforce_Q_symmetric))(A, B, Q, R)
    )


def _largest_common_dtype(tensors: Sequence[TensorVariable]) -> np.dtype:
    return reduce(lambda l, r: np.promote_types(l, r), [x.dtype for x in tensors])


class BaseBlockDiagonal(Op):
    __props__ = ("n_inputs",)

    def __init__(self, n_inputs):
        input_sig = ",".join(f"(m{i},n{i})" for i in range(n_inputs))
        self.gufunc_signature = f"{input_sig}->(m,n)"

        if n_inputs == 0:
            raise ValueError("n_inputs must be greater than 0")
        self.n_inputs = n_inputs

    def grad(self, inputs, gout):
        shapes = pt.stack([i.shape for i in inputs])
        index_end = shapes.cumsum(0)
        index_begin = index_end - shapes
        slices = [
            ptb.ix_(
                pt.arange(index_begin[i, 0], index_end[i, 0]),
                pt.arange(index_begin[i, 1], index_end[i, 1]),
            )
            for i in range(len(inputs))
        ]
        return [gout[0][slc] for slc in slices]

    def infer_shape(self, fgraph, nodes, shapes):
        first, second = zip(*shapes, strict=True)
        return [(pt.add(*first), pt.add(*second))]

    def _validate_and_prepare_inputs(self, matrices, as_tensor_func):
        if len(matrices) != self.n_inputs:
            raise ValueError(
                f"Expected {self.n_inputs} matri{'ces' if self.n_inputs > 1 else 'x'}, got {len(matrices)}"
            )
        matrices = list(map(as_tensor_func, matrices))
        if any(mat.type.ndim != 2 for mat in matrices):
            raise TypeError("All inputs must have dimension 2")
        return matrices


class BlockDiagonal(BaseBlockDiagonal):
    __props__ = ("n_inputs",)

    def make_node(self, *matrices):
        matrices = self._validate_and_prepare_inputs(matrices, pt.as_tensor)
        dtype = _largest_common_dtype(matrices)

        shapes_by_dim = tuple(zip(*(m.type.shape for m in matrices)))
        out_shape = tuple(
            [
                sum(dim_shapes)
                if not any(shape is None for shape in dim_shapes)
                else None
                for dim_shapes in shapes_by_dim
            ]
        )

        out_type = pytensor.tensor.matrix(shape=out_shape, dtype=dtype)
        return Apply(self, matrices, [out_type])

    def perform(self, node, inputs, output_storage, params=None):
        dtype = node.outputs[0].type.dtype
        output_storage[0][0] = scipy_linalg.block_diag(*inputs).astype(dtype)


def block_diag(*matrices: TensorVariable):
    """
    Construct a block diagonal matrix from a sequence of input tensors.

    Given the inputs `A`, `B` and `C`, the output will have these arrays arranged on the diagonal:

    [[A, 0, 0],
     [0, B, 0],
     [0, 0, C]]

    Parameters
    ----------
    A, B, C ... : tensors
        Input tensors to form the block diagonal matrix. last two dimensions of the inputs will be used, and all
        inputs should have at least 2 dimensins.

    Returns
    -------
    out: tensor
        The block diagonal matrix formed from the input matrices.

    Examples
    --------
    Create a block diagonal matrix from two 2x2 matrices:

    ..code-block:: python

        import numpy as np
        from pytensor.tensor.linalg import block_diag

        A = pt.as_tensor_variable(np.array([[1, 2], [3, 4]]))
        B = pt.as_tensor_variable(np.array([[5, 6], [7, 8]]))

        result = block_diagonal(A, B, name='X')
        print(result.eval())
        Out: array([[1, 2, 0, 0],
                     [3, 4, 0, 0],
                     [0, 0, 5, 6],
                     [0, 0, 7, 8]])
    """
    _block_diagonal_matrix = Blockwise(BlockDiagonal(n_inputs=len(matrices)))
    return _block_diagonal_matrix(*matrices)


class QR(Op):
    """
    QR Decomposition
    """

    __props__ = (
        "overwrite_a",
        "mode",
        "pivoting",
        "check_finite",
    )

    def __init__(
        self,
        mode: Literal["full", "r", "economic", "raw"] = "full",
        overwrite_a: bool = False,
        pivoting: bool = False,
        check_finite: bool = False,
    ):
        self.mode = mode
        self.overwrite_a = overwrite_a
        self.pivoting = pivoting
        self.check_finite = check_finite

        self.destroy_map = {}

        if overwrite_a:
            self.destroy_map = {0: [0]}

        match self.mode:
            case "economic":
                self.gufunc_signature = "(m,n)->(m,k),(k,n)"
            case "full":
                self.gufunc_signature = "(m,n)->(m,m),(m,n)"
            case "r":
                self.gufunc_signature = "(m,n)->(m,n)"
            case "raw":
                self.gufunc_signature = "(m,n)->(n,m),(k),(m,n)"
            case _:
                raise ValueError(
                    f"Invalid mode '{mode}'. Supported modes are 'full', 'economic', 'r', and 'raw'."
                )

        if pivoting:
            self.gufunc_signature += ",(n)"

    def make_node(self, x):
        x = as_tensor_variable(x)

        assert x.ndim == 2, "The input of qr function should be a matrix."

        # Preserve static shape information if possible
        M, N = x.type.shape
        if M is not None and N is not None:
            K = min(M, N)
        else:
            K = None

        in_dtype = x.type.numpy_dtype
        out_dtype = np.dtype(f"f{in_dtype.itemsize}")

        match self.mode:
            case "full":
                outputs = [
                    tensor(shape=(M, M), dtype=out_dtype),
                    tensor(shape=(M, N), dtype=out_dtype),
                ]
            case "economic":
                outputs = [
                    tensor(shape=(M, K), dtype=out_dtype),
                    tensor(shape=(K, N), dtype=out_dtype),
                ]
            case "r":
                outputs = [
                    tensor(shape=(M, N), dtype=out_dtype),
                ]
            case "raw":
                outputs = [
                    tensor(shape=(M, M), dtype=out_dtype),
                    tensor(shape=(K,), dtype=out_dtype),
                    tensor(shape=(M, N), dtype=out_dtype),
                ]
            case _:
                raise NotImplementedError

        if self.pivoting:
            outputs = [*outputs, tensor(shape=(N,), dtype="int32")]

        return Apply(self, [x], outputs)

    def infer_shape(self, fgraph, node, shapes):
        (x_shape,) = shapes

        M, N = x_shape
        K = ptm.minimum(M, N)

        Q_shape = None
        R_shape = None
        tau_shape = None
        P_shape = None

        match self.mode:
            case "full":
                Q_shape = (M, M)
                R_shape = (M, N)
            case "economic":
                Q_shape = (M, K)
                R_shape = (K, N)
            case "r":
                R_shape = (M, N)
            case "raw":
                Q_shape = (M, M)  # Actually this is H in this case
                tau_shape = (K,)
                R_shape = (M, N)

        if self.pivoting:
            P_shape = (N,)

        return [
            shape
            for shape in (Q_shape, tau_shape, R_shape, P_shape)
            if shape is not None
        ]

    def inplace_on_inputs(self, allowed_inplace_inputs: list[int]) -> "Op":
        if not allowed_inplace_inputs:
            return self
        new_props = self._props_dict()  # type: ignore
        new_props["overwrite_a"] = True
        return type(self)(**new_props)

    def _call_and_get_lwork(self, fn, *args, lwork, **kwargs):
        if lwork in [-1, None]:
            *_, work, info = fn(*args, lwork=-1, **kwargs)
            lwork = work.item()

        return fn(*args, lwork=lwork, **kwargs)

    def perform(self, node, inputs, outputs):
        (x,) = inputs
        M, N = x.shape

        if self.pivoting:
            (geqp3,) = get_lapack_funcs(("geqp3",), (x,))
            qr, jpvt, tau, *work_info = self._call_and_get_lwork(
                geqp3, x, lwork=-1, overwrite_a=self.overwrite_a
            )
            jpvt -= 1  # geqp3 returns a 1-based index array, so subtract 1
        else:
            (geqrf,) = get_lapack_funcs(("geqrf",), (x,))
            qr, tau, *work_info = self._call_and_get_lwork(
                geqrf, x, lwork=-1, overwrite_a=self.overwrite_a
            )

        if self.mode not in ["economic", "raw"] or M < N:
            R = np.triu(qr)
        else:
            R = np.triu(qr[:N, :])

        if self.mode == "r" and self.pivoting:
            outputs[0][0] = R
            outputs[1][0] = jpvt
            return

        elif self.mode == "r":
            outputs[0][0] = R
            return

        elif self.mode == "raw" and self.pivoting:
            outputs[0][0] = qr
            outputs[1][0] = tau
            outputs[2][0] = R
            outputs[3][0] = jpvt
            return

        elif self.mode == "raw":
            outputs[0][0] = qr
            outputs[1][0] = tau
            outputs[2][0] = R
            return

        (gor_un_gqr,) = get_lapack_funcs(("orgqr",), (qr,))

        if M < N:
            Q, work, info = self._call_and_get_lwork(
                gor_un_gqr, qr[:, :M], tau, lwork=-1, overwrite_a=1
            )
        elif self.mode == "economic":
            Q, work, info = self._call_and_get_lwork(
                gor_un_gqr, qr, tau, lwork=-1, overwrite_a=1
            )
        else:
            t = qr.dtype.char
            qqr = np.empty((M, M), dtype=t)
            qqr[:, :N] = qr

            # Always overwite qqr -- it's a meaningless intermediate value
            Q, work, info = self._call_and_get_lwork(
                gor_un_gqr, qqr, tau, lwork=-1, overwrite_a=1
            )

        outputs[0][0] = Q
        outputs[1][0] = R

        if self.pivoting:
            outputs[2][0] = jpvt

    def L_op(self, inputs, outputs, output_grads):
        """
        Reverse-mode gradient of the QR function.

        References
        ----------
        .. [1] Jinguo Liu. "Linear Algebra Autodiff (complex valued)", blog post https://giggleliu.github.io/posts/2019-04-02-einsumbp/
        .. [2] Hai-Jun Liao, Jin-Guo Liu, Lei Wang, Tao Xiang. "Differentiable Programming Tensor Networks", arXiv:1903.09650v2
        """

        from pytensor.tensor.slinalg import solve_triangular

        (A,) = (cast(ptb.TensorVariable, x) for x in inputs)
        m, n = A.shape

        # Check if we have static shape info, if so we can get a better graph (avoiding the ifelse Op in the output)
        M_static, N_static = A.type.shape
        shapes_unknown = M_static is None or N_static is None

        def _H(x: ptb.TensorVariable):
            return x.conj().mT

        def _copyltu(x: ptb.TensorVariable):
            return ptb.tril(x, k=0) + _H(ptb.tril(x, k=-1))

        if self.mode == "raw":
            raise NotImplementedError("Gradient of qr not implemented for mode=raw")

        elif self.mode == "r":
            k = pt.minimum(m, n)

            # We need all the components of the QR to compute the gradient of A even if we only
            # use the upper triangular component in the cost function.
            props_dict = self._props_dict()
            props_dict["mode"] = "economic"
            props_dict["pivoting"] = False

            qr_op = type(self)(**props_dict)

            Q, R = qr_op(A)
            dQ = Q.zeros_like()

            # Unlike numpy.linalg.qr, scipy.linalg.qr returns the full (m,n) matrix when mode='r', *not* the (k,n)
            # matrix that is computed by mode='economic'. The gradient assumes that dR is of shape (k,n), so we need to
            # slice it to the first k rows. Note that if m <= n, then k = m, so this is safe in all cases.
            dR = cast(ptb.TensorVariable, output_grads[0][:k, :])

        else:
            Q, R = (cast(ptb.TensorVariable, x) for x in outputs)
            if self.mode == "full":
                qr_assert_op = Assert(
                    "Gradient of qr not implemented for m x n matrices with m > n and mode=full"
                )
                R = qr_assert_op(R, ptm.le(m, n))

            new_output_grads = []
            is_disconnected = [
                isinstance(x.type, DisconnectedType) for x in output_grads
            ]
            if all(is_disconnected):
                # This should never be reached by Pytensor
                return [DisconnectedType()()]  # pragma: no cover

            for disconnected, output_grad, output in zip(
                is_disconnected, output_grads, [Q, R], strict=True
            ):
                if disconnected:
                    new_output_grads.append(output.zeros_like())
                else:
                    new_output_grads.append(output_grad)

            (dQ, dR) = (cast(ptb.TensorVariable, x) for x in new_output_grads)

        if shapes_unknown or M_static >= N_static:
            # gradient expression when m >= n
            M = R @ _H(dR) - _H(dQ) @ Q
            K = dQ + Q @ _copyltu(M)
            A_bar_m_ge_n = _H(solve_triangular(R, _H(K)))

            if not shapes_unknown:
                return [A_bar_m_ge_n]

        # We have to trigger both branches if shapes_unknown is True, so this is purposefully not an elif branch
        if shapes_unknown or M_static < N_static:
            # gradient expression when m < n
            Y = A[:, m:]
            U = R[:, :m]
            dU, dV = dR[:, :m], dR[:, m:]
            dQ_Yt_dV = dQ + Y @ _H(dV)
            M = U @ _H(dU) - _H(dQ_Yt_dV) @ Q
            X_bar = _H(solve_triangular(U, _H(dQ_Yt_dV + Q @ _copyltu(M))))
            Y_bar = Q @ dV
            A_bar_m_lt_n = pt.concatenate([X_bar, Y_bar], axis=1)

            if not shapes_unknown:
                return [A_bar_m_lt_n]

        return [ifelse(ptm.ge(m, n), A_bar_m_ge_n, A_bar_m_lt_n)]


def qr(
    A: TensorLike,
    mode: Literal["full", "r", "economic", "raw", "complete", "reduced"] = "full",
    overwrite_a: bool = False,
    pivoting: bool = False,
    lwork: int | None = None,
):
    """
    QR Decomposition of input matrix `a`.

    The QR decomposition of a matrix `A` is a factorization of the form :math`A = QR`, where `Q` is an orthogonal
    matrix (:math:`Q Q^T = I`) and `R` is an upper triangular matrix.

    This decomposition is useful in various numerical methods, including solving linear systems and least squares
    problems.

    Parameters
    ----------
    A: TensorLike
        Input matrix of shape (M, N) to be decomposed.

    mode: str, one of "full", "economic", "r", or "raw"
        How the QR decomposition is computed and returned. Choosing the mode can avoid unnecessary computations,
        depending on which of the return matrices are needed. Given input matrix with shape  Choices are:

            - "full" (or "complete"): returns `Q` and `R` with dimensions `(M, M)` and `(M, N)`.
            - "economic" (or "reduced"): returns `Q` and `R` with dimensions `(M, K)` and `(K, N)`,
                                         where `K = min(M, N)`.
            - "r": returns only `R` with dimensions `(K, N)`.
            - "raw": returns `H` and `tau` with dimensions `(N, M)` and `(K,)`, where `H` is the matrix of
                     Householder reflections, and tau is the vector of Householder coefficients.

    pivoting: bool, default False
        If True, also return a vector of rank-revealing permutations `P` such that `A[:, P] = QR`.

    overwrite_a: bool, ignored
        Ignored. Included only for consistency with the function signature of `scipy.linalg.qr`. Pytensor will always
        automatically overwrite the input matrix `A` if it is safe to do sol.

    lwork: int, ignored
        Ignored. Included only for consistency with the function signature of `scipy.linalg.qr`. Pytensor will
        automatically determine the optimal workspace size for the QR decomposition.

    Returns
    -------
    Q or H: TensorVariable, optional
        A matrix with orthonormal columns. When mode = 'complete', it is the result is an orthogonal/unitary matrix
        depending on whether a is real/complex. The determinant may be either +/- 1 in that case. If
        mode = 'raw', it is the matrix of Householder reflections. If mode = 'r', Q is not returned.

    R or tau : TensorVariable, optional
        Upper-triangular matrix. If mode = 'raw', it is the vector of Householder coefficients.

    """
    # backwards compatibility from the numpy API
    if mode == "complete":
        mode = "full"
    elif mode == "reduced":
        mode = "economic"

    return Blockwise(QR(mode=mode, pivoting=pivoting, overwrite_a=False))(A)


__all__ = [
    "cholesky",
    "solve",
    "eigvalsh",
    "expm",
    "solve_discrete_lyapunov",
    "solve_continuous_lyapunov",
    "solve_discrete_are",
    "solve_triangular",
    "block_diag",
    "cho_solve",
    "lu",
    "lu_factor",
    "lu_solve",
    "qr",
]
