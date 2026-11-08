# Social Justice Hackathon 2026

## How to setup and run the project

To install packages run:

```shell
uv sync
```

To setup the project:

1. Install postgres and postgis.
2. Change the Database urls in the project.
3. Run load.py on provided-resources/AIS-data.csv
4. Optionally export you aisstream.io api key to AISSTREAM_API_KEY=...

To run fastapi

```shell
uv run fastapi dev
```
