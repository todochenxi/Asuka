# TTL

```json metadata
{
  "schema_version": 2,
  "title": "TTL",
  "description": "Returns the expiration time in seconds of a key.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"}],
  "syntax_fmt": "TTL key",
  "complexity": "O(1)",
  "group": "generic",
  "command_flags": ["readonly","fast"],
  "acl_categories": ["@keyspace","@read","@fast"],
  "since": "1.0.0",
  "arity": 2,
  "key_specs": [{"RO":true,"access":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"}}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_generic-stepttl","commands":[{"acl_categories":["@write","@string","@slow"],"complexity":"O(1)","name":"SET"},{"acl_categories":["@keyspace","@write","@fast"],"complexity":"O(1)","name":"EXPIRE"},{"acl_categories":["@keyspace","@read","@fast"],"complexity":"O(1)","name":"TTL"}],"description":"Foundational: Check remaining time-to-live of a key using TTL (returns seconds remaining, -1 if no expiry, -2 if key doesn\u0026amp;#39;t exist)","difficulty":"beginner","id":"ttl","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_generic-stepttl"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_generic-stepttl"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_generic-stepttl"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_generic-stepttl"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_generic-stepttl"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_generic-stepttl"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_generic-stepttl"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_generic-stepttl"},{"clientId":"hiredis","clientName":"hiredis","id":"C","langId":"c","panelId":"panel_C_cmds_generic-stepttl"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_generic-stepttl"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_generic-stepttl"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_generic-stepttl"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_generic-stepttl"}]}]
}
```

## Code Examples Legend

The code examples below show how to perform the same operations in different programming languages and client libraries:

- **Redis CLI**: Command-line interface for Redis
- **C# (Synchronous)**: StackExchange.Redis synchronous client
- **C# (Asynchronous)**: StackExchange.Redis asynchronous client
- **Go**: go-redis client
- **Java (Synchronous - Jedis)**: Jedis synchronous client
- **Java (Asynchronous - Lettuce)**: Lettuce asynchronous client
- **Java (Reactive - Lettuce)**: Lettuce reactive/streaming client
- **JavaScript (Node.js)**: node-redis client
- **PHP**: Predis client
- **Python**: redis-py client
- **Rust (Synchronous)**: redis-rs synchronous client
- **Rust (Asynchronous)**: redis-rs asynchronous client

Each code example demonstrates the same basic operation across different languages. The specific syntax and patterns vary based on the language and client library, but the underlying Redis commands and behavior remain consistent.

---

Returns the remaining time to live of a key that has a timeout.
This introspection capability allows a Redis client to check how many seconds a
given key will continue to be part of the dataset.

In Redis 2.6 or older the command returns `-1` if the key does not exist or if the key exists but has no associated expire.

Starting with Redis 2.8 the return value in case of error changed:

* The command returns `-2` if the key does not exist.
* The command returns `-1` if the key exists but has no associated expire.

See also the [`PTTL`](https://redis.io/docs/latest/commands/pttl) command that returns the same information with milliseconds resolution (Only available in Redis 2.6 or greater).

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key.

</details>

## Examples

Foundational: Check remaining time-to-live of a key using TTL (returns seconds remaining, -1 if no expiry, -2 if key doesn't exist)

**Difficulty:** Beginner

**Commands:** SET, EXPIRE, TTL

**Complexity:**
- SET: O(1)
- EXPIRE: O(1)
- TTL: O(1)

**Available in:** Redis CLI, C, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> SET mykey "Hello"
OK
> EXPIRE mykey 10
(integer) 1
> TTL mykey
(integer) 10
```

##### C

```c
    reply = redisCommand(c, "SET mykey Hello");
    printf("%s\n", reply->str);
    // >>> OK
    freeReplyObject(reply);

    reply = redisCommand(c, "EXPIRE mykey 10");
    printf("%lld\n", reply->integer);
    // >>> 1
    freeReplyObject(reply);

    reply = redisCommand(c, "TTL mykey");
    printf("%lld\n", reply->integer);
    // >>> 10
    freeReplyObject(reply);
```

##### C#

```csharp
        bool ttlResult1 = db.StringSet("mykey", "Hello");
        Console.WriteLine(ttlResult1);  // >>> true

        bool ttlResult2 = db.KeyExpire("mykey", new TimeSpan(0, 0, 10));
        Console.WriteLine(ttlResult2);

        TimeSpan ttlResult3 = db.KeyTimeToLive("mykey") ?? TimeSpan.Zero;
        string ttlRes = Math.Round(ttlResult3.TotalSeconds).ToString();
        Console.WriteLine(Math.Round(ttlResult3.TotalSeconds)); // >>> 10
```

##### Go

```go
	ttlResult1, err := rdb.Set(ctx, "mykey", "Hello", 10*time.Second).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(ttlResult1) // >>> OK

	ttlResult2, err := rdb.TTL(ctx, "mykey").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(math.Round(ttlResult2.Seconds())) // >>> 10
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> ttlExample = asyncCommands.set("mykey", "Hello")
                    .thenCompose(r1 -> {
                        System.out.println(r1);              // >>> OK
                        return asyncCommands.expire("mykey", 10);
                    })
                    .thenCompose(r2 -> {
                        System.out.println(r2);              // >>> true
                        return asyncCommands.ttl("mykey");
                    })
                    .thenAccept(r3 -> {
                        System.out.println(r3);              // >>> 10
                    })
                    .toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> ttlExample = reactiveCommands.set("mykey", "Hello")
                    .flatMap(r1 -> {
                        System.out.println(r1);              // >>> OK
                        return reactiveCommands.expire("mykey", 10);
                    })
                    .flatMap(r2 -> {
                        System.out.println(r2);              // >>> true
                        return reactiveCommands.ttl("mykey");
                    })
                    .doOnNext(r3 -> {
                        System.out.println(r3);              // >>> 10
                    })
                    .then();
```

##### Java (Synchronous - Jedis)

```java
        String ttlResult1 = jedis.set("mykey", "Hello");
        System.out.println(ttlResult1); // >>> OK

        long ttlResult2 = jedis.expire("mykey", 10);
        System.out.println(ttlResult2); // >>> 1

        long ttlResult3 = jedis.ttl("mykey");
        System.out.println(ttlResult3); // >>> 10
```

##### JavaScript (Node.js)

```javascript
const ttlRes1 = await client.set('mykey', 'Hello');
console.log(ttlRes1); // OK

const ttlRes2 = await client.expire('mykey', 10);
console.log(ttlRes2); // 1

const ttlRes3 = await client.ttl('mykey');
console.log(ttlRes3); // 10
```

##### JavaScript (Node.js)

```javascript
console.log(await redis.set('mykey', 'Hello')); // >>> OK
console.log(await redis.expire('mykey', 10)); // >>> 1

const ttlResult = await redis.ttl('mykey');
console.log(ttlResult); // >>> 10
```

##### JavaScript (Node.js)

```javascript
const ttlRes1 = await client.set('mykey', 'Hello');
console.log(ttlRes1); // OK

const ttlRes2 = await client.expire('mykey', 10);
console.log(ttlRes2); // 1

const ttlRes3 = await client.ttl('mykey');
console.log(ttlRes3); // 10
```

##### JavaScript (Node.js)

```javascript
console.log(await redis.set('mykey', 'Hello')); // >>> OK
console.log(await redis.expire('mykey', 10)); // >>> 1

const ttlResult = await redis.ttl('mykey');
console.log(ttlResult); // >>> 10
```

##### PHP

```php
        echo $r->set('mykey', 'Hello') . PHP_EOL;            // >>> OK
        echo $r->expire('mykey', 10) . PHP_EOL;              // >>> 1

        $ttlResult = $r->ttl('mykey');
        echo $ttlResult . PHP_EOL;                           // >>> 10
```

##### Python

```python
res = r.set("mykey", "Hello")
print(res)
# >>> True

res = r.expire("mykey", 10)
print(res)
# >>> True

res = r.ttl("mykey")
print(res)
# >>> 10
```

##### Rust (Asynchronous)

```rust
        if let Ok(res) = r.set("mykey", "Hello").await {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.expire("mykey", 10).await {
            Ok(res) => {
                let res: bool = res;
                println!("{res}");    // >>> true
            },
            Err(e) => {
                println!("Error setting key expiration: {e}");
                return;
            }
        }

        match r.ttl("mykey").await {
            Ok(res) => {
                let res: i64 = res;
                println!("{res}");    // >>> 10
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }
```

##### Rust (Synchronous)

```rust
        if let Ok(res) = r.set("mykey", "Hello") {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.expire("mykey", 10) {
            Ok(res) => {
                let res: bool = res;
                println!("{res}");    // >>> true
            },
            Err(e) => {
                println!("Error setting key expiration: {e}");
                return;
            }
        }

        match r.ttl("mykey") {
            Ok(res) => {
                let res: i64 = res;
                println!("{res}");    // >>> 10
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }
```



## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

One of the following:
* [Integer reply](../../develop/reference/protocol-spec#integers): TTL in seconds.
* [Integer reply](../../develop/reference/protocol-spec#integers): `-1` if the key exists but has no associated expiration.
* [Integer reply](../../develop/reference/protocol-spec#integers): `-2` if the key does not exist.

**RESP3:**

One of the following:
* [Integer reply](../../develop/reference/protocol-spec#integers): TTL in seconds.
* [Integer reply](../../develop/reference/protocol-spec#integers): `-1` if the key exists but has no associated expiration.
* [Integer reply](../../develop/reference/protocol-spec#integers): `-2` if the key does not exist.



