# SMEMBERS

```json metadata
{
  "schema_version": 2,
  "title": "SMEMBERS",
  "description": "Returns all members of a set.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"}],
  "syntax_fmt": "SMEMBERS key",
  "complexity": "O(N) where N is the set cardinality.",
  "group": "set",
  "command_flags": ["readonly"],
  "acl_categories": ["@read","@set","@slow"],
  "since": "1.0.0",
  "arity": 2,
  "key_specs": [{"RO":true,"access":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"}}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_set-stepsmembers","commands":[{"acl_categories":["@write","@set","@fast"],"complexity":"O(1)","name":"SADD"},{"acl_categories":["@read","@set","@slow"],"complexity":"O(N)","name":"SMEMBERS"}],"description":"Foundational: Retrieve all members of a set using SMEMBERS (returns unordered collection, useful for iterating all set members)","difficulty":"beginner","id":"smembers","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_set-stepsmembers"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_set-stepsmembers"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_set-stepsmembers"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_set-stepsmembers"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_set-stepsmembers"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_set-stepsmembers"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_set-stepsmembers"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_set-stepsmembers"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_set-stepsmembers"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_set-stepsmembers"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_set-stepsmembers"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_set-stepsmembers"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_set-stepsmembers"}]}]
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

Returns all the members of the set value stored at `key`.

This has the same effect as running [`SINTER`](https://redis.io/docs/latest/commands/sinter) with one argument `key`.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key that holds the set.

</details>

## Examples

Foundational: Retrieve all members of a set using SMEMBERS (returns unordered collection, useful for iterating all set members)

**Difficulty:** Beginner

**Commands:** SADD, SMEMBERS

**Complexity:**
- SADD: O(1)
- SMEMBERS: O(N)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
redis> SADD myset "Hello"
(integer) 1
redis> SADD myset "World"
(integer) 1
redis> SMEMBERS myset
1) "Hello"
2) "World"
```

##### C#

```csharp
        long sMembersResult1 = db.SetAdd(
            "myset", ["Hello", "World"]
        );
        Console.WriteLine(sMembersResult1); // >>> 2

        RedisValue[] sMembersResult2 = db.SetMembers("myset");
        Array.Sort(sMembersResult2);
        Console.WriteLine(string.Join(", ", sMembersResult2));
        // >>> Hello, World
```

##### Go

```go
	sAddResult, err := rdb.SAdd(ctx, "myset", "Hello", "World").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(sAddResult) // >>> 2

	sMembersResult, err := rdb.SMembers(ctx, "myset").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(sMembersResult) // >>> [Hello World]
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> smembers = asyncCommands.sadd("myset", "Hello", "World").thenCompose(r -> {
                return asyncCommands.smembers("myset");
            })
                    .thenAccept(System.out::println)
                    // >>> [Hello, World]
                    .toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> smembers = reactiveCommands.sadd("myset", "Hello", "World").doOnNext(r -> {
                System.out.println(r); // >>> 2
            }).flatMap(r -> reactiveCommands.smembers("myset").collectList()).doOnNext(r -> {
                System.out.println(r); // >>> [Hello, World]
            }).then();
```

##### Java (Synchronous - Jedis)

```java
        long sMembersResult1 = jedis.sadd("myset", "Hello", "World");
        System.out.println(sMembersResult1); // >>> 2

        Set<String> sMembersResult2 = jedis.smembers("myset");
        System.out.println(sMembersResult2.stream().sorted().collect(toList()));
        // >>> [Hello, World]
```

##### JavaScript (Node.js)

```javascript
const res4 = await client.sAdd('myset', ['Hello', 'World']);
console.log(res4);  // 2

const res5 = await client.sMembers('myset')
console.log(res5);  // ['Hello', 'World']

```

##### JavaScript (Node.js)

```javascript
const res4 = await redis.sadd('myset', 'Hello', 'World');
console.log(res4); // >>> 2

const res5 = await redis.smembers('myset');
console.log(res5); // >>> ['Hello', 'World']
```

##### JavaScript (Node.js)

```javascript
const res4 = await client.sAdd('myset', ['Hello', 'World']);
console.log(res4);  // 2

const res5 = await client.sMembers('myset')
console.log(res5);  // ['Hello', 'World']

```

##### JavaScript (Node.js)

```javascript
const res4 = await redis.sadd('myset', 'Hello', 'World');
console.log(res4); // >>> 2

const res5 = await redis.smembers('myset');
console.log(res5); // >>> ['Hello', 'World']
```

##### PHP

```php
        $res4 = $r->sadd('myset', ['Hello', 'World']);
        echo $res4 . PHP_EOL;
        // >>> 2

        $res5 = $r->smembers('myset');
        sort($res5);
        echo json_encode($res5) . PHP_EOL;
        // >>> ["Hello","World"]
```

##### Python

```python
res4 = r.sadd("myset", "Hello", "World")
print(res4)  # >>> 2

res5 = r.smembers("myset")
print(res5)  # >>> {'Hello', 'World'}

```

##### Ruby

```ruby
res4 = r.sadd('myset', ['Hello', 'World'])
puts res4 # >>> 2

res5 = r.smembers('myset')
puts res5.inspect # >>> ["Hello", "World"]
```

##### Rust (Asynchronous)

```rust
        if let Ok(res4) = r.sadd("myset", &["Hello", "World"]).await {
            let res4: i32 = res4;
            println!("{res4}"); // >>> 2
        }

        if let Ok(res5) = r.smembers("myset").await {
            let res5: HashSet<String> = res5;
            println!("{res5:?}"); // >>> {"Hello", "World"}
        }
```

##### Rust (Synchronous)

```rust
        if let Ok(res4) = r.sadd("myset", &["Hello", "World"]) {
            let res4: i32 = res4;
            println!("{res4}"); // >>> 2
        }

        if let Ok(res5) = r.smembers("myset") {
            let res5: HashSet<String> = res5;
            println!("{res5:?}"); // >>> {"Hello", "World"}
        }
```



## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Array reply](../../develop/reference/protocol-spec#arrays): an array with all the members of the set.

**RESP3:**

[Set reply](../../develop/reference/protocol-spec#sets): a set with all the members of the set.



