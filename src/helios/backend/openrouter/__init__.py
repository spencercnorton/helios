"""GTK-free, fail-closed OpenRouter gateway primitives."""

from .gateway import (
    CHAT_COMPLETIONS_URL,
    CancellationToken,
    GatewayError,
    GatewayErrorKind,
    OpenRouterGateway,
)
from .models import (
    CancelResult,
    CancelStatus,
    EndpointRef,
    InferenceRequest,
    InferenceResult,
    MessageRole,
    ProfileRef,
    ProfileStatus,
    PromptMessage,
    ReadOnlyModelProfile,
    RouteReceipt,
    UsageReceipt,
)
from .profiles import CATALOG_SNAPSHOT, PROFILES, get_profile
from .transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportFailure,
    TransportFailureKind,
    UrlLibTransport,
)

__all__ = [
    "CATALOG_SNAPSHOT",
    "CHAT_COMPLETIONS_URL",
    "PROFILES",
    "CancelResult",
    "CancelStatus",
    "CancellationToken",
    "EndpointRef",
    "GatewayError",
    "GatewayErrorKind",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "InferenceRequest",
    "InferenceResult",
    "MessageRole",
    "OpenRouterGateway",
    "ProfileRef",
    "ProfileStatus",
    "PromptMessage",
    "ReadOnlyModelProfile",
    "RouteReceipt",
    "TransportFailure",
    "TransportFailureKind",
    "UsageReceipt",
    "UrlLibTransport",
    "get_profile",
]

