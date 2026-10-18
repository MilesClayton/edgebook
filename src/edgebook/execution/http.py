"""Authenticated JSON-only routes on the existing persistent service."""

from __future__ import annotations

import hmac

from aiohttp import web
from pydantic import ValidationError

from edgebook.execution.live_ledger import (
    ConfirmRequest,
    GuardError,
    Limits,
    ProposalRequest,
    ReviewRequest,
)
from edgebook.execution.service import ExecutionService


def add_routes(
    app: web.Application,
    service: ExecutionService | None,
    operator_token: str,
    proposal_token: str,
    disabled_reason: str = "Execution setup is disabled on this server.",
) -> None:
    async def handle(request: web.Request) -> web.Response:
        action = request.match_info["action"]
        expected = proposal_token if action in {"review", "propose"} else operator_token
        supplied = request.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(
            supplied.encode(), f"Bearer {expected}".encode()
        ):
            return web.json_response({"error": "authentication required"}, status=401)
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if service is None:
            return web.json_response(
                {
                    "configured": False,
                    "enabled": False,
                    "paused": True,
                    "unattended_enabled": False,
                    "submitted": False,
                    "reason": disabled_reason,
                },
                status=200 if action == "status" else 503,
                headers=headers,
            )
        try:
            if action == "status" and request.method == "GET":
                result = await service.status()
            elif action in {
                "review",
                "propose",
                "confirm",
                "pause",
                "resume",
                "reconcile",
                "limits",
                "autopilot",
                "longshot",
            } and (request.method == "POST"):
                if request.content_type != "application/json":
                    raise GuardError("application/json required")
                payload = await request.json()
                if action in {"limits", "autopilot", "longshot"}:
                    key = "limits" if action == "limits" else "enabled"
                    if (
                        not isinstance(payload, dict)
                        or set(payload) != {"confirmed", key}
                        or payload["confirmed"] is not True
                    ):
                        raise GuardError("explicit control confirmation required")
                    if action == "limits":
                        result = await service.set_limits(Limits.model_validate(payload["limits"]))
                    elif not isinstance(payload["enabled"], bool):
                        raise GuardError("enabled must be true or false")
                    elif action == "longshot":
                        result = await service.set_longshot(payload["enabled"])
                    else:
                        result = await service.set_autopilot(payload["enabled"])
                    return web.json_response(result, headers=headers)
                if action == "review":
                    result = await service.review(ReviewRequest.model_validate(payload))
                elif action == "propose":
                    result = await service.propose(ProposalRequest.model_validate(payload))
                elif action == "confirm":
                    result = await service.confirm(ConfirmRequest.model_validate(payload))
                else:
                    if (
                        not isinstance(payload, dict)
                        or set(payload) != {"confirmed"}
                        or payload["confirmed"] is not True
                    ):
                        raise GuardError("explicit control confirmation required")
                    result = await service.control(action)
            else:
                return web.json_response({"error": "unknown route or method"}, status=404)
            return web.json_response(result, headers=headers)
        except (ValidationError, ValueError):
            return web.json_response(
                {"error": "invalid request; exact schema required"}, status=400, headers=headers
            )
        except GuardError as error:
            service.ledger.audit(
                service.clock(),
                "request_rejected",
                {
                    "route": action,
                    "reason": str(error),
                },
            )
            return web.json_response(
                {
                    "error": str(error),
                    **(
                        {"outcome": "check_backend_status_before_retrying"}
                        if action == "confirm"
                        else {"submitted": False}
                    ),
                },
                status=409,
                headers=headers,
            )
        except Exception:
            service.ledger.pause(service.clock(), "service unavailable; reconciliation required")
            return web.json_response(
                {
                    "error": "Execution unavailable. Check order status before retrying.",
                },
                status=503,
                headers=headers,
            )

    app.router.add_route("*", "/execution/{action}", handle)
