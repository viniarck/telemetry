"""Main module of kytos/telemetry Network Application.

Napp to deploy In-band Network Telemetry over Ethernet Virtual Circuits

"""
import itertools
from collections import defaultdict

from tenacity import RetryError

from kytos.core import KytosNApp, rest, log
from kytos.core.rest_api import (
    HTTPException,
    JSONResponse,
    Request,
    aget_json_or_400,
)

from .managers.int import INTManager
from .exceptions import (
    EVCHasNoINT,
    EVCHasINT,
    EVCNotFound,
    FlowsNotFound,
    ProxyPortNotFound,
    ProxyPortStatusNotUP,
    UnrecoverableError,
)
from .kytos_api_helper import get_evc, get_evcs
from .proxy_port import ProxyPort
from .utils import (
    get_evc_unis,
    get_evc_with_telemetry,
    get_proxy_port_or_raise,
    has_int_enabled,
)

# pylint: disable=fixme


class Main(KytosNApp):
    """Main class of kytos/telemetry NApp.

    This class is the entry point for this NApp.
    """

    def setup(self):
        """Replace the '__init__' method for the KytosNApp subclass.

        The setup method is automatically called by the controller when your
        application is loaded.

        So, if you have any setup routine, insert it here.
        """

        self.int_manager = INTManager(self.controller)

    def execute(self):
        """Run after the setup method execution.

        You can also use this method in loop mode if you add to the above setup
        method a line like the following example:

            self.execute_as_loop(30)  # 30-second interval.
        """

    def shutdown(self):
        """Run when your NApp is unloaded.

        If you have some cleanup procedure, insert it here.
        """

    async def provision_int_unidirectional(
        self, evc: dict, source_uni: dict, destination_uni: dict, proxy_port: ProxyPort
    ) -> dict[str, list]:
        """Create INT flows from source to destination."""
        switches_flows = defaultdict(list)

        # Create flows for the first switch (INT Source)
        source_flows = self.enable_int_source(source_uni, evc, proxy_port)

        # Create flows the INT hops
        hop_flows = self.enable_int_hop(evc, source_uni, destination_uni)

        # # Create flows for the last switch (INT Sink)
        sink_flows = self.enable_int_sink(destination_uni, evc, proxy_port)

        for flow in itertools.chain(source_flows, hop_flows, sink_flows):
            switches_flows[flow["switch"]].append(flow)

        return await self.install_int_flows(switches_flows)

    async def provision_int(self, evc: dict) -> str:
        """Create telemetry flows for an EVC."""
        # TODO change this to a list for dispatching in bulk

        # Get the EVC endpoints
        evc_id = evc["id"]
        uni_a, uni_z = get_evc_unis(evc)
        uni_a_intf_id, uni_z_intf_id = uni_a["interface_id"], uni_z["interface_id"]

        uni_a_proxy_port = get_proxy_port_or_raise(self.controller, uni_a_intf_id)
        uni_z_proxy_port = get_proxy_port_or_raise(self.controller, uni_z_intf_id)

        await self.provision_int_unidirectional(evc, uni_z, uni_a, uni_a_proxy_port)
        await self.provision_int_unidirectional(evc, uni_a, uni_z, uni_z_proxy_port)

        # set_telemetry_true_for_evc(evc_id, "bidirectional")
        return "enabled"

    def decommission_int(self, evc: dict) -> str:
        """Remove all INT flows for an EVC"""

        evc_id = evc["id"]
        self.remove_int_flows(evc)

        # Update mef_eline.
        # if not set_telemetry_false_for_evc(evc_id):
        #     raise ErrorBase(evc_id, "failed to disable telemetry metadata")

        return f"EVC ID {evc_id} is no longer INT-enabled."

    @rest("v1/evc/enable", methods=["POST"])
    async def enable_telemetry(self, request: Request) -> JSONResponse:
        """REST to enable INT flows on EVCs.

        If a list of evc_ids is empty, it'll enable on non-INT EVCs.
        """

        try:
            content = await aget_json_or_400(request)
            evc_ids = content["evc_ids"]
            force = bool(content.get("force", False))
        except (TypeError, KeyError):
            raise HTTPException(400, detail=f"Invalid payload: {content}")

        try:
            evcs = await get_evcs() if len(evc_ids) != 1 else await get_evc(evc_ids[0])
        except RetryError as exc:
            exc_error = str(exc.last_attempt.exception())
            log.error(exc_error)
            raise HTTPException(503, detail=exc_error)

        if evc_ids:
            evcs = {evc_id: evcs.get(evc_id, {}) for evc_id in evc_ids}
        else:
            evcs = {k: v for k, v in evcs.items() if not has_int_enabled(v)}
            if not evcs:
                # There's no non-INT EVCs to get enabled.
                return JSONResponse({})

        try:
            await self.int_manager.enable_int(evcs, force)
        except (EVCNotFound, FlowsNotFound, ProxyPortNotFound) as exc:
            raise HTTPException(404, detail=str(exc))
        except (EVCHasINT, ProxyPortStatusNotUP) as exc:
            raise HTTPException(400, detail=str(exc))
        except RetryError as exc:
            exc_error = str(exc.last_attempt.exception())
            log.error(exc_error)
            raise HTTPException(503, detail=exc_error)
        except UnrecoverableError as exc:
            exc_error = str(exc)
            log.error(exc_error)
            raise HTTPException(500, detail=exc_error)

        return JSONResponse({}, status_code=201)

    @rest("v1/evc/disable", methods=["POST"])
    async def disable_telemetry(self, request: Request) -> JSONResponse:
        """REST to disable/remove INT flows for an EVC_ID

        If a list of evc_ids is empty, it'll disable on all INT EVCs.
        """
        try:
            content = await aget_json_or_400(request, self.controller.loop)
            evc_ids = content["evc_ids"]
            force = bool(content.get("force", False))
        except (TypeError, KeyError):
            raise HTTPException(400, detail=f"Invalid payload: {content}")

        try:
            evcs = await get_evcs() if len(evc_ids) != 1 else await get_evc(evc_ids[0])
        except RetryError as exc:
            exc_error = str(exc.last_attempt.exception())
            log.error(exc_error)
            raise HTTPException(503, detail=exc_error)

        if evc_ids:
            evcs = {evc_id: evcs.get(evc_id, {}) for evc_id in evc_ids}
        else:
            evcs = {k: v for k, v in evcs.items() if has_int_enabled(v)}
            if not evcs:
                # There's no INT EVCs to get disabled.
                return JSONResponse({})

        try:
            await self.int_manager.disable_int(evcs, force)
        except EVCNotFound as exc:
            raise HTTPException(404, detail=str(exc))
        except EVCHasNoINT as exc:
            raise HTTPException(400, detail=str(exc))
        except RetryError as exc:
            exc_error = str(exc.last_attempt.exception())
            log.error(exc_error)
            raise HTTPException(503, detail=exc_error)
        except UnrecoverableError as exc:
            exc_error = str(exc)
            log.error(exc_error)
            raise HTTPException(500, detail=exc_error)

        return JSONResponse({})

    @rest("v1/evc")
    def get_evcs(self, _request: Request) -> JSONResponse:
        """REST to return the list of EVCs with INT enabled"""
        return JSONResponse(get_evc_with_telemetry())

    @rest("v1/sync")
    def sync_flows(self, _request: Request) -> JSONResponse:
        """Endpoint to force the telemetry napp to search for INT flows and delete them
        accordingly to the evc metadata."""

        # TODO
        # for evc_id in get_evcs_ids():
        return JSONResponse("TBD")

    @rest("v1/evc/update")
    def update_evc(self, _request: Request) -> JSONResponse:
        """If an EVC changed from unidirectional to bidirectional telemetry,
        make the change."""
        return JSONResponse({})

    # Event-driven methods: future
    def listen_for_new_evcs(self):
        """Change newly created EVC to INT-enabled EVC based on the metadata field
        (future)"""
        pass

    def listen_for_evc_change(self):
        """Change newly created EVC to INT-enabled EVC based on the
        metadata field (future)"""
        pass

    def listen_for_path_changes(self):
        """Change EVC's new path to INT-enabled EVC based on the metadata field
        when there is a path change. (future)"""
        pass

    def listen_for_evcs_removed(self):
        """Remove all INT flows belonging the just removed EVC (future)"""
        pass

    def listen_for_topology_changes(self):
        """If the topology changes, make sure it is not the loop ports.
        If so, update proxy ports"""
        # TODO:
        # self.proxy_ports = create_proxy_ports(self.proxy_ports)
        pass
