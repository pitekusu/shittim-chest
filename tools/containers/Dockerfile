# Canonical upstream images for local tests and the CI/release builder.
# Consumers read these named stages without rebuilding the upstream images.
# Dependabot updates their tags and immutable digests together.
FROM amazon/dynamodb-local@sha256:6ada631ec7aa5fc83446855eb17233e3dad184fec47477e8140c6c569cfe0c86 AS dynamodb-local
FROM moby/buildkit:v0.35.0@sha256:aa632b921ef07411d2cb77292cd34213d4f7b384001d6fceef1e1f25bf9752bb AS buildkit
