from sanic import Sanic

from service.blue_print import bp as bp_bp

app = Sanic("TerminalGatewayService")

app.blueprint(bp_bp)

app.config.HEALTH = True
app.config.HEALTH_ENDPOINT = True
app.config.CORS_ORIGINS = "*"
app.config.OAS = False
app.config.OAS_AUTODOC = False
app.config.REQUEST_TIMEOUT = 300
app.config.RESPONSE_TIMEOUT = 300
app.config.NOISY_EXCEPTIONS = True
app.config.KEEP_ALIVE_TIMEOUT = 300
app.config.REQUEST_BUFFER_SIZE = 131072
app.config.REQUEST_MAX_HEADER_SIZE = 24576
app.config.FALLBACK_ERROR_FORMAT = "json"
