# The Shittim Chest

The Shittim Chest is a design-stage Discord multi-agent debate Bot. A moderator
coordinates three configurable participant slots, shared evidence, revised
proposals, anonymous voting, and a mechanically calculated result through the
`/shittim` command.

## Status

Requirements and detailed design are available under [`docs/`](docs/).
Application code, Discord Applications, AWS resources, and production workflows
have not been implemented yet.

The planned runtime uses Python, Discord, the OpenAI Responses API, DynamoDB,
and one ARM64 ECS Fargate Spot task in the Tokyo Region. Fargate Spot
interruption is handled with checkpoints, fenced leases, and an outbox.

## Public and private boundaries

This repository contains generic slots and public-safe design information only.
Production Guild/channel/Application IDs, display names, persona prompts, Bot
tokens, and API keys are not stored here. Runtime configuration is designed to
be loaded from versioned AWS Systems Manager Parameter Store parameters.

Report security issues through GitHub's private vulnerability reporting rather
than a public Issue. See [SECURITY.md](SECURITY.md).

## Contributing and license

Implementation contributions are welcome; read [CONTRIBUTING.md](CONTRIBUTING.md).
Source code, infrastructure as code, tools, and samples are licensed under the
MIT License. Design documents and `AGENTS.md` are excluded; see
[LICENSE-SCOPE.md](LICENSE-SCOPE.md).

