import requests


class EKaro:
    API_URL = "https://ekaro-api.affiliaters.in/api/converter/public"

    def __init__(self, token):
        self.token = token

    def convert_url(self, url: str) -> str:
        payload = {
            "deal": url,
            "convert_option": "convert_only"
        }

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(
                self.API_URL,
                json=payload,
                headers=headers,
                timeout=30
            )

            response.raise_for_status()

            data = response.json()

            if data.get("success"):
                return data["data"].strip()

        except Exception as e:
            print(e)

        return url