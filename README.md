# Open Geospatial Copilot

An open-source, AI-powered geospatial assistant for exploring, analyzing, and visualizing Earth data using natural language queries. This project is based on the original Earth Copilot by Microsoft, but is Azure-free.

## Project Description

Open Geospatial Copilot enables users to:
- Search and visualize satellite and geospatial data collections with natural language
- Run locally via Docker, without any Azure dependencies
- Extend and customize backend and frontend modules for new data sources or workflows

The project is inspired by and reuses open source patterns from [Earth Copilot](https://github.com/microsoft/Earth-Copilot), but is not affiliated with or endorsed by Microsoft. See the credit section below for attribution.

## Query Examples

| Query |
|-------|
| Show me high resolution satellite imagery of Dubai urban expansion in 2020 |
| Show me radar imagery of Houston Texas during Hurricane Harvey August 2017 |
| Show me HLS Landsat imagery for Ukraine farmland from 2024 |
| Show me burned area mapping for Montana wildfire regions 2023 |
| Show me NDVI vegetation health for Iowa cropland summer 2024 |
| Show me sea surface temperature anomalies in the Gulf of Mexico |

## Quick Deploy

See [QUICK_DEPLOY.md](QUICK_DEPLOY.md)

## Future Features

The `open-geospatial-copilot/ai-search` directory contains scripts and setup for semantic (vector-based) search. These files are currently not used in the deployed application, but are retained for future development when vector AI search is implemented (e.g., with PostGIS or Parquet). For now, only the STAC API is active.

The `open-geospatial-copilot/mcp-server` directory contains code and configuration for an optional Model Context Protocol (MCP) server. This server is designed for advanced AI/LLM context management, agent orchestration, and integration with external tools or workflows. It is not required for basic usage or local deployments, but can be used if you need multi-agent coordination, persistent conversation context, or a backend bridge for complex AI workflows.

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
