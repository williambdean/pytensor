import warnings

import mlx.core as mx

from pytensor.link.mlx.dispatch.basic import mlx_funcify
from pytensor.tensor.slinalg import Cholesky, Solve, SolveTriangular


@mlx_funcify.register(Cholesky)
def mlx_funcify_Cholesky(op, node, **kwargs):
    lower = op.lower
    out_dtype = getattr(mx, node.outputs[0].dtype)

    def cholesky(a):
        return mx.linalg.cholesky(a, upper=not lower, stream=mx.cpu).astype(
            out_dtype, stream=mx.cpu
        )

    return cholesky


@mlx_funcify.register(Solve)
def mlx_funcify_Solve(op, node, **kwargs):
    assume_a = op.assume_a
    out_dtype = getattr(mx, node.outputs[0].dtype)

    if assume_a != "gen":
        warnings.warn(
            f"MLX solve does not support assume_a={op.assume_a}. Defaulting to assume_a='gen'.\n"
            f"If appropriate, you may want to set assume_a to one of 'sym', 'pos', 'her' or 'tridiagonal' to improve performance.",
            UserWarning,
        )

    def solve(a, b):
        # MLX only supports solve on CPU
        return mx.linalg.solve(a, b, stream=mx.cpu).astype(out_dtype, stream=mx.cpu)

    return solve


@mlx_funcify.register(SolveTriangular)
def mlx_funcify_SolveTriangular(op, node, **kwargs):
    lower = op.lower
    out_dtype = getattr(mx, node.outputs[0].dtype)

    def solve_triangular(A, b):
        return mx.linalg.solve_triangular(
            A,
            b,
            upper=not lower,
            stream=mx.cpu,  # MLX only supports solve_triangular on CPU
        ).astype(out_dtype, stream=mx.cpu)

    return solve_triangular
