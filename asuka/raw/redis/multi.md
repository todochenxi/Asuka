# MULTI

```json metadata
{
  "schema_version": 2,
  "title": "MULTI",
  "description": "Starts a transaction.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "syntax_fmt": "MULTI",
  "complexity": "O(1)",
  "group": "transactions",
  "command_flags": ["noscript","loading","stale","fast","allow_busy"],
  "acl_categories": ["@fast","@transaction"],
  "since": "1.2.0",
  "arity": 1,
  "tableOfContents": {"sections":[{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": []
}
```

> [!NOTE]
> This command's behavior varies in clustered Redis environments. See the [multi-key operations](https://redis.io/docs/latest/develop/using-commands/multi-key-operations) page for more information.


Marks the start of a [transaction](https://redis.io/docs/latest/develop/using-commands/transactions) block.
Subsequent commands will be queued for atomic execution using [`EXEC`](https://redis.io/docs/latest/commands/exec).

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Simple string reply](../../develop/reference/protocol-spec#simple-strings): `OK`.

**RESP3:**

[Simple string reply](../../develop/reference/protocol-spec#simple-strings): `OK`.



