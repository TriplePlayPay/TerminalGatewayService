import aiohttp

from sanic import json, Request

from service.logger import get_logger

logger = get_logger()


def get_api_key_from_http_request(request: Request) -> str:
    """
    Returns the API Key from a given request
    :param request:
    :return:
    """
    def remove_bearer_prefix_case_insensitive(s):
        prefix = "bearer "
        if s.lower().startswith(prefix):
            return s[len(prefix) :]
        return s

    api_key = remove_bearer_prefix_case_insensitive(
        request.headers.get("Authorization")
    )
    logger.info(f"api_key value gotten from authorization: {api_key}")
    return api_key


async def get_authorization_from_api_key(api_key: str) -> bool:
    """
    Authorizes a key against our primary server.

    Returns a boolean to let us know if the caller is authorized or not.

    :param api_key: str
    :return: bool
    """

    data = {"auth_key": api_key}
    headers = {"Authorization": f"Bearer {api_key}"}
    logger.info(f"Submitting data for authorization: {data}")
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post('https://tripleplaypay.com/authorized', json=data) as response:

            if response.status != 200:
                return False

            the_json = await response.json()

            return the_json.get("authorized", False)
