# WATCH

```json metadata
{
  "schema_version": 2,
  "title": "WATCH",
  "description": "Monitors changes to keys to determine the execution of a transaction.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"multiple":true,"name":"key","type":"key"}],
  "syntax_fmt": "WATCH key [key ...]",
  "complexity": "O(1) for every key.",
  "group": "transactions",
  "command_flags": ["noscript","loading","stale","fast","allow_busy"],
  "acl_categories": ["@fast","@transaction"],
  "since": "2.2.0",
  "arity": -2,
  "key_specs": [{"RO":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":-1,"limit":0},"type":"range"}}],
  "tableOfContents": {"sections":[{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": []
}
```

> [!NOTE]
> This command's behavior varies in clustered Redis environments. See the [multi-key operations](https://redis.io/docs/latest/develop/using-commands/multi-key-operations) page for more information.


Marks the given keys to be watched for conditional execution of a
[transaction](https://redis.io/docs/latest/develop/using-commands/transactions).

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Simple string reply](../../develop/reference/protocol-spec#simple-strings): `OK`.

**RESP3:**

[Simple string reply](../../develop/reference/protocol-spec#simple-strings): `OK`.



