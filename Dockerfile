# Blast Radius selector. The base image is the one the tests were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The labeling environment (requirements.lock from the starter kit), so that
# tests can be run here exactly as they were labeled.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.lock

# sqlglot-src.tar.xz: sqlglot/, tests/ (with fixtures) and sqlglotc/ at the pinned
# commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa (see tools/build.sh).
# map.pkl.xz / durations.json: coverage, import, reference and runtime maps built
# once from the same commit.
COPY selector.py brrun.py brserver.py durations.json map.pkl.xz sqlglot-src.tar.xz /app/
RUN python -c "import tarfile; tarfile.open('/app/sqlglot-src.tar.xz').extractall('/opt/sqlglot', filter='data')" \
 && rm /app/sqlglot-src.tar.xz \
 && chmod -R a+rX /app /opt/sqlglot

ENV BR_SRC=/opt/sqlglot BR_MAP=/app/map.pkl.xz PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-I", "-S", "/app/selector.py"]
