# Canonical upstream images for local tests and the CI/release builder.
# Consumers read these named stages without rebuilding the upstream images.
# Dependabot updates their tags and immutable digests together.
FROM amazon/dynamodb-local@sha256:6ada631ec7aa5fc83446855eb17233e3dad184fec47477e8140c6c569cfe0c86 AS dynamodb-local
FROM moby/buildkit:v0.34.2@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe AS buildkit
