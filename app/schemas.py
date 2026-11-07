from typing import Annotated

from pydantic import BaseModel, HttpUrl, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class HelpdeskWebhook(BaseModel):
    event_id: NonEmptyStr
    ticket_id: NonEmptyStr
    customer_email: NonEmptyStr
    message: NonEmptyStr
    reply_url: HttpUrl


class WebhookAccepted(BaseModel):
    status: str = "accepted"
    duplicate: bool
