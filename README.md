# OpenGeo AI Assistant

An open-source, AI-powered geospatial assistant for exploring, analyzing, and visualizing Earth data using natural language queries. This project is based on the original Earth Copilot by Microsoft, but is Azure-free.

## Project Description

OpenGeo AI Assistant enables users to:

- Search and visualize satellite and geospatial data collections with natural language
- Run locally via Docker, without any Azure dependencies
- Extend and customize backend and frontend modules for new data sources or workflows

The project is inspired by and reuses open source patterns from [Earth Copilot](https://github.com/microsoft/Earth-Copilot), but is not affiliated with or endorsed by Microsoft. See the credit section below for attribution.

## Differences from the Original Project

This project extends and adapts the original Earth Copilot codebase with the following changes:

- Removed all dependencies to Microsoft Azure tools:
  - Azure OpenAI -> LLM is now decided by the user with `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` environemnt variables
  - Azure Maps -> Leaflet and other open-source tools

- Implemented custom large language model (LLM) options instead of relying solely on OpenAI
- Implemented Open-Meteo API

## Quick Deploy

See [QUICK_DEPLOY.md](QUICK_DEPLOY.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## Future Features

The `opengeo-ai-assistant/ai-search` directory contains scripts and setup for semantic (vector-based) search. These files are currently not used in the deployed application, but are retained for future development when vector AI search is implemented (e.g., with PostGIS or Parquet). For now, only the STAC API is active.

The `opengeo-ai-assistant/mcp-server` directory contains code and configuration for an optional Model Context Protocol (MCP) server. This server is designed for advanced AI/LLM context management, agent orchestration, and integration with external tools or workflows. It is not required for basic usage or local deployments, but can be used if you need multi-agent coordination, persistent conversation context, or a backend bridge for complex AI workflows.

## License

See [LICENSE.txt](LICENSE.txt)

## Credits & Acknowledgments

This project is based on the open-source Earth Copilot project originally published by Microsoft.

For more information, see the official presentation:
[Satya Nadella introduces NASA Earth Copilot 1.0 at Microsoft Ignite 2024](https://www.linkedin.com/posts/microsoft_msignite-activity-7265061510635241472-CAYx/?utm_source=share&utm_medium=member_desktop)

The code, architecture, and documentation are inspired by the initial open-source repository. Thanks to all contributors of the original project. [See original repository](https://github.com/microsoft/Earth-Copilot/tree/main)

Earth Copilot was developed by Melisa Bardhi and advised by Juan Carlos Lopez.

The original Earth Copilot project involved contributions and collaboration from:

- Microsoft Planetary Computer  
- NASA  
- Microsoft team members including Juan Carlos Lopez, Jocelynn Hartwig, Minh Nguyen, and Matt Morrell.

This project is an independent work and is not affiliated with, endorsed by, or sponsored by Microsoft or NASA.
