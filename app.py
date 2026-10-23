from flask import Flask
from modules.blueprints import create_app

app: Flask = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
