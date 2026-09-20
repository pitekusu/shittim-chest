# Canonical upstream images for local tests and the CI/release builder.
# Consumers read these named stages without rebuilding the upstream images.
# Dependabot updates their tags and immutable digests together.
FROM amazon/dynamodb-local@sha256:d89f8fcc6b1a39cb35976c248ed42a28c66ae00dc043099210f5571e42648ab4 AS dynamodb-local
FROM moby/buildkit:v0.33.0@sha256:6c2fa84a6b61ccd72899dde4239f8d5717f05f9a8ca6f3cad185fb1a95a94de3 AS buildkit
