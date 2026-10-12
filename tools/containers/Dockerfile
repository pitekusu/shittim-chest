# Canonical upstream images for local tests and the CI/release builder.
# Consumers read these named stages without rebuilding the upstream images.
# Dependabot updates their tags and immutable digests together.
FROM amazon/dynamodb-local@sha256:ff89bd48ff32cd8d9be5fee8873b65b8854dc408f1afe881be6eb00247bc0dab AS dynamodb-local
FROM moby/buildkit:v0.34.0@sha256:b059f8d7226d0b326bc871af489a5dddf65e36af4d20d14251b072974d9ee87c AS buildkit
