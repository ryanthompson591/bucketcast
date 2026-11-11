"""App Engine entry point (also used for local dev: `flask --app main run`)."""

from bucketcast import create_app

app = create_app()
