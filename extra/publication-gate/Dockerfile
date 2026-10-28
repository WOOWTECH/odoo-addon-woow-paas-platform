FROM python:3.12.7-slim@sha256:0cce4bfc26c646e941c07e3b7b282f40b476e96b8ad82a224e24e9b3ff331776
WORKDIR /opt/publication-gate
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY publication_gate ./publication_gate
USER 65532:65532
ENTRYPOINT ["python", "-m", "publication_gate.main"]
