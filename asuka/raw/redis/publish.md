# PUBLISH

```json metadata
{
  "schema_version": 2,
  "title": "PUBLISH",
  "description": "Posts a message to a channel.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"channel","name":"channel","type":"string"},{"display_text":"message","name":"message","type":"string"}],
  "syntax_fmt": "PUBLISH channel message",
  "complexity": "O(N+M) where N is the number of clients subscribed to the receiving channel and M is the total number of subscribed patterns (by any client).",
  "group": "pubsub",
  "command_flags": ["pubsub","loading","stale","fast"],
  "acl_categories": ["@pubsub","@fast"],
  "since": "2.0.0",
  "arity": 3,
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": []
}
```

Posts a message to the given channel.

In a Redis Cluster, clients can publish to every node. The cluster makes sure
that published messages are forwarded as needed, so clients can subscribe to any
channel by connecting to any one of the nodes.

## Required arguments

<details open><summary><code>channel</code></summary>

The channel to publish to.

</details>

<details open><summary><code>message</code></summary>

The message to publish.

</details>

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Integer reply](../../develop/reference/protocol-spec#integers): the number of clients that the message was sent to. Note that in a Redis Cluster, only clients that are connected to the same node as the publishing client are included in the count.

**RESP3:**

[Integer reply](../../develop/reference/protocol-spec#integers): the number of clients that the message was sent to. Note that in a Redis Cluster, only clients that are connected to the same node as the publishing client are included in the count.



