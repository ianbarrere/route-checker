#!/usr/bin/env python3
import netmiko  # type: ignore
import re
import click  # type: ignore
import datetime
import json
import yaml  # type: ignore
import os
from time import sleep
from functools import partial
from ntc_templates.parse import parse_output  # type: ignore
from typing import (
    Optional,
    Dict,
    TypedDict,
    List,
    Literal,
    Callable,
    Pattern,
    Match,
    get_args,
    no_type_check,
)
from slack_sdk import WebClient  # type: ignore

HOSTNAME = "route-views.routeviews.org"
USERNAME = "rviews"
FILENAME = "/tmp/route_view.log"
OUTPUT_TYPES = Literal["normal", "verbose", "alert", "json"]


class View(TypedDict):
    timestamp: str
    contents: Dict[str, List[int]]


PEER_TRANSLATE = {3257: "GTT", 6939: "HURRICANE"}
PREFIX_TRANSLATE = {
    "1.2.3.0/24": "MY_SITE",
}


def post_to_channel(channel: str, path: str, comment: str) -> None:
    """
    Common function for posting a file to a slack channel
    """
    assert os.environ.get(
        "SLACK_API_TOKEN"
    ), "The 'SLACK_API_TOKEN' environment variable must be set"

    client = WebClient(token=os.environ["SLACK_API_TOKEN"])
    response = client.files_upload_v2(
        channels=channel, file=path, initial_comment=comment
    )
    if not response["ok"]:
        print(f"Failed to post text snippet. Error: {response['error']}")


class RouteView:
    """
    Object for fetching and reporting on route view query
    """

    def __init__(
        self,
        hostname: str,
        username: str,
        asn: str,
        intent_file: str,
        refresh: bool = False,
    ):
        self.hostname = hostname
        self.username = username
        self.asn = asn
        self.refresh = refresh
        self.view = self._get_view()
        self.intent_file = intent_file
        self.intent = yaml.safe_load(open(intent_file))

    def _view_from_route_server(self) -> None:
        """
        Query view from route server and write to log file
        """
        retry = 5
        success = False
        while not success and retry >= 1:
            try:
                self.connection = netmiko.ConnectHandler(
                    host=self.hostname,
                    username=self.username,
                    device_type="cisco_ios_telnet",
                )
                success = True
            except netmiko.exceptions.NetmikoAuthenticationException:
                sleep(3)
                retry -= 1
                continue
        if not success:
            raise RuntimeError("Error logging into route-views after 5 attempts")
        output = re.sub(
            "\n[NV]",
            "\n",
            self.connection.send_command(
                f"show ip bgp regexp _{self.asn}", read_timeout=120
            ),
        )
        parsed = parse_output(platform="cisco_ios", command="show ip bgp", data=output)
        timestamp = datetime.datetime.now().isoformat()
        # sort list of prefixes so we get a consistent diff
        prefix_list = [item["network"] for item in parsed]
        prefix_list.sort()
        # define data with keys from sorted list
        data: Dict[str, List[int]] = {prefix: [] for prefix in prefix_list}
        # populate values with AS numbers
        for item in parsed:
            pattern = re.compile(f"(\w+) {self.asn}")  # type: Pattern[str]
            match = pattern.search(item["as_path"])  # type: Optional[Match[str]]
            if isinstance(match, re.Match):
                data[item["network"]].append(int(str(match.groups()[0])))

        # cast paths to dict keys and back to list to remove duplicates
        for network, path in data.items():
            path.sort()
            data[network] = list(dict.fromkeys(path))

        report = {"timestamp": timestamp, "contents": data}

        with open(FILENAME, "a") as file:
            file.write(f"{json.dumps(report)}\n")

    # ignore type checking here since we work around the variability of the return value
    # in code
    @no_type_check
    def _view_from_file(self) -> View:
        """
        Read latest view from log file
        """

        def _read_file() -> Optional[View]:
            """
            Small wrapper for file reading, mostly to allow for calling twice in a row
            if first run returns None.
            """
            with open(FILENAME, "r") as file:
                lines = file.readlines()
                if not lines:
                    return None
                latest_view_str = lines[-1]
                latest_view = json.loads(latest_view_str)
                return latest_view

        if not os.path.exists(FILENAME):
            self._view_from_route_server()

        latest_view = _read_file()
        if not latest_view:
            self._view_from_route_server()
            latest_view = _read_file()
        return latest_view

    def _get_view(self) -> View:
        """
        Builds route view and returns standardized model
        """
        latest_view = self._view_from_file()
        if not latest_view or self.refresh:
            self._view_from_route_server()
        else:
            view_time = datetime.datetime.fromisoformat(latest_view["timestamp"])
            time_now = datetime.datetime.now()
            if view_time < (time_now - datetime.timedelta(hours=8)):
                self._view_from_route_server()
        return self._view_from_file()

    @staticmethod
    def _get_path(path: List[int]) -> str:
        """
        Build string path from set
        """
        return ", ".join(
            [
                f"{path} ({PEER_TRANSLATE[path]})"
                if path in PEER_TRANSLATE
                else f"{path} (UNKNOWN AS)"
                for path in path
            ]
        )

    def view_to_slack(
        self, channel: str, content: str, alerts_only: bool = True
    ) -> None:
        """
        Write view to file and upload to slack channel, takes content argument so that
        various formats can be supplied and alerts_only to suppress OK state when run
        from schedule
        """
        if not "CRITICAL" in content and alerts_only:
            return
        filename = f"/tmp/{datetime.datetime.now().isoformat()}_route_view.txt"
        with open(filename, "w") as file:
            file.write(content)
        post_to_channel(channel, filename, "route-view")

    def verbose_view(self) -> str:
        """
        Print method for verbose
        """
        output = ""
        for prefix, path in self.view["contents"].items():
            string_path = self._get_path(path)
            output += (
                f"Prefix {prefix} ({PREFIX_TRANSLATE.get(prefix)}) is "
                f"accessible via next hop AS {string_path}\n"
            )
        return output

    def alert_view(self) -> str:
        """
        Print method for nagios-compatible alert
        """
        errors = False
        output = ""
        for prefix, path in self.view["contents"].items():
            if prefix not in self.intent:
                errors = True
                output += f"Prefix {prefix} not found in intent file! "
                continue
            if self.intent[prefix] != path:
                errors = True
                output += (
                    f"Prefix {prefix} intent {self.intent[prefix]} does not "
                    f"match reality {path}  "
                )
        if not errors:
            output += "No alerts!"
        return f"[CRITICAL] {output}" if errors else f"[OK] {output}"

    def normal_view(self) -> str:
        """
        Print method for normal
        """
        output = ""
        for prefix, path in self.view["contents"].items():
            string_path = ", ".join(
                [PEER_TRANSLATE.get(path, str(path)) for path in path]
            )
            output += (
                f"{PREFIX_TRANSLATE.get(prefix, prefix)} prefix found via next hop AS "
                f"{string_path}\n"
            )
        return output


main = click.Group(help="Route Checker")


@main.command()
@click.argument("asn")
@click.option("-S", "--slack", "channel", help="Post report to slack")
@click.option(
    "-A",
    "--alerts-only",
    "alerts_only",
    help="Suppress non critical from Slack output",
    is_flag=True,
    default=False,
)
@click.option(
    "-O",
    "--output-type",
    "output_type",
    type=click.Choice(get_args(OUTPUT_TYPES)),
    default="normal",
)
@click.option(
    "-R", "--refresh", "refresh", is_flag=True, help="Force a refresh of the view"
)
@click.option(
    "-I",
    "--intent_file",
    "intent_file",
    help="Use intent file from path",
    default="route_intent.yaml",
)
def show(
    asn: str,
    output_type: OUTPUT_TYPES,
    channel: str,
    alerts_only: bool,
    refresh: bool,
    intent_file: str,
) -> None:
    """
    Show command for route-view. Displays various attributes of routes.

    ASN: AS number of your organization
    """

    def _parse_output(content: str, slack_func: Optional[Callable]):
        """
        Helper function to output message properly. Will send to slack if slack_func
        is not None, otherwise it will echo to stdout
        """
        if slack_func:
            slack_func(content=content)
            return
        click.secho(content)
        return

    routes = RouteView(
        hostname=HOSTNAME,
        username=USERNAME,
        asn=asn,
        refresh=refresh,
        intent_file=intent_file,
    )

    to_slack = None
    if channel:
        to_slack = partial(routes.view_to_slack, channel, alerts_only=alerts_only)
    if output_type == "json":
        _parse_output(content=str(routes.view), slack_func=to_slack)
    elif output_type == "verbose":
        _parse_output(content=routes.verbose_view(), slack_func=to_slack)
    elif output_type == "alert":
        _parse_output(content=routes.alert_view(), slack_func=to_slack)
    else:
        _parse_output(content=routes.normal_view(), slack_func=to_slack)


if __name__ == "__main__":
    main()
