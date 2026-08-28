FROM python:3.13-slim

WORKDIR /app

# NetCDF/HDF5 reads here are single-file and single-threaded; letting NumPy's
# BLAS backend grab every core buys nothing and just adds contention.
#
# MPLCONFIGDIR: the container runs as the host UID, which has no home directory
# here, so matplotlib (pulled in by shared/render.py) would otherwise warn and
# rebuild its font cache on every invocation.
#
# The virtualenv lives OUTSIDE /app on purpose: docker-compose bind-mounts
# ./process over /app for live editing, which would hide anything installed
# under it.
ENV OMP_NUM_THREADS=1 \
	OPENBLAS_NUM_THREADS=1 \
	MKL_NUM_THREADS=1 \
	NUMEXPR_NUM_THREADS=1 \
	HDF5_USE_FILE_LOCKING=FALSE \
	PYTHONUNBUFFERED=1 \
	MPLCONFIGDIR=/tmp/matplotlib \
	UV_PROJECT_ENVIRONMENT=/opt/venv \
	PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir uv

COPY process/pyproject.toml ./
RUN uv sync --no-dev

COPY process .
COPY shared ./shared

# docker-compose may pass a host UID via `user:`; keep /app writable for it.
RUN chmod -R a+rwX /app

# CMD ["sleep", "infinity"]
