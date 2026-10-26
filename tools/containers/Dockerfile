# Canonical upstream images for local tests and the CI/release builder.
# Consumers read these named stages without rebuilding the upstream images.
# Dependabot updates their tags and immutable digests together.
FROM amazon/dynamodb-local@sha256:6ada631ec7aa5fc83446855eb17233e3dad184fec47477e8140c6c569cfe0c86 AS dynamodb-local
FROM moby/buildkit:v0.34.1@sha256:99368f3e46177c6405b9740e4461d91cee004128daeecca78d4ceac50ec1da5c AS buildkit
