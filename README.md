# Route-checker

CLI tool for checking route transit accessibility via route-views looking glass.

Stores a snapshot at /tmp/route_view.log, if snapshot is older than 8 hours and the script
is run again it will fetch a new snapshot and run against that. A variety of output
formats are available. The script's help function clarifies most of these.

## Intent file
In order to compare the current view against how things should be you need to maintain
the intent file. The example intent file looks like this:

    1.2.3.0/23:
    - 3356
    - 6461
    1.2.4.0/24:
    - 174
    - 6939

The data is serialized in YAML and consists of your network prefixes as keys with values
as a list of next-hop AS numbers. The example file indicates that you have two prefixes
advertised to the Internet: 1.2.3.0/24 and 1.2.4.0/24. The first prefix is accessible
via Lumen (3356) and Zayo (6461), the second is accessible via Cogent (174) and Hurricane
Electric (6939).

**Note** that this is not the AS path, but simply a list of next-hop AS numbers immediately
upstream from your network.

## Slack integration
You can populate the SLACK_API_TOKEN environment variable and then pass a slack channel
ID with the -S option in order to feed the output to a slack channel. You can combine
this argument with different output types, like -O alert and --alerts-only to forward
only critical events if run from a schedule.

Posting to Slack creates a timestamped file under /tmp to attach to the channel rather
than inputting directly to improve readability.

## Data translation
There are two translation tables in code:

    PEER_TRANSLATE = {
        3257: "GTT",
        6939: "HURRICANE"
    }
    PREFIX_TRANSLATE = {
        "1.2.3.0/24": "MY_SITE",
    }

Which can be updated to translate AS numbers and prefixes into more friendly values. The
first table translates upstream provider AS numbers into org names, the latter translates
your own prefixes into site names.