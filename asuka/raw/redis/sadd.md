# SADD

```json metadata
{
  "schema_version": 2,
  "title": "SADD",
  "description": "Adds one or more members to a set. Creates the key if it doesn't exist.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"},{"display_text":"member","multiple":true,"name":"member","type":"string"}],
  "syntax_fmt": "SADD key member [member ...]",
  "complexity": "O(1) for each element added, so O(N) to add N elements when the command is called with multiple arguments.",
  "group": "set",
  "command_flags": ["write","denyoom","fast"],
  "acl_categories": ["@write","@set","@fast"],
  "since": "1.0.0",
  "arity": -3,
  "key_specs": [{"RW":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"},"insert":true}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_set-stepsadd","commands":[{"acl_categories":["@write","@set","@fast"],"complexity":"O(1)","name":"SADD"},{"acl_categories":["@read","@set","@slow"],"complexity":"O(N)","name":"SMEMBERS"}],"description":"Foundational: Add one or more members to a set using SADD (creates set if needed, ignores duplicates, returns count of new members)","difficulty":"beginner","id":"sadd","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_set-stepsadd"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_set-stepsadd"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_set-stepsadd"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_set-stepsadd"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_set-stepsadd"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_set-stepsadd"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_set-stepsadd"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_set-stepsadd"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_set-stepsadd"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_set-stepsadd"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_set-stepsadd"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_set-stepsadd"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_set-stepsadd"}]}]
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

Add the specified members to the set stored at `key`.
Specified members that are already a member of this set are ignored.
If `key` does not exist, a new set is created before adding the specified
members.

An error is returned when the value stored at `key` is not a set.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key that holds the set.

</details>

<details open><summary><code>member [member ...]</code></summary>

One or more members to add to the set.

</details>

## Examples

Foundational: Add one or more members to a set using SADD (creates set if needed, ignores duplicates, returns count of new members)

**Difficulty:** Beginner

**Commands:** SADD, SMEMBERS

**Complexity:**
- SADD: O(1)
- SMEMBERS: O(N)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
redis> SADD myset "Hello" "World"
(integer) 2
redis> SADD myset "World"
(integer) 0
redis> SMEMBERS myset
1) "Hello"
2) "World"
```

##### C#

```csharp
        bool sAddResult1 = db.SetAdd("myset", "Hello");
        Console.WriteLine(sAddResult1); // >>> True

        bool sAddResult2 = db.SetAdd("myset", "World");
        Console.WriteLine(sAddResult2); // >>> True

        bool sAddResult3 = db.SetAdd("myset", "World");
        Console.WriteLine(sAddResult2); // >>> False

        RedisValue[] sAddResult4 = db.SetMembers("myset");
        Array.Sort(sAddResult4);
        Console.WriteLine(string.Join(", ", sAddResult4));
        // >>> Hello, World
```

##### Go

```go
	sAddResult1, err := rdb.SAdd(ctx, "myset", "Hello").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(sAddResult1) // >>> 1

	sAddResult2, err := rdb.SAdd(ctx, "myset", "World").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(sAddResult2) // >>> 1

	sAddResult3, err := rdb.SAdd(ctx, "myset", "World").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(sAddResult3) // >>> 0

	sMembersResult, err := rdb.SMembers(ctx, "myset").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(sMembersResult) // >>> [Hello World]
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> sadd = asyncCommands.sadd("myset", "Hello").thenCompose(r -> {
                System.out.println(r); // >>> 1
                return asyncCommands.sadd("myset", "World");
            }).thenCompose(r -> {
                System.out.println(r); // >>> 1
                return asyncCommands.sadd("myset", "World");
            }).thenCompose(r -> {
                System.out.println(r); // >>> 0
                return asyncCommands.smembers("myset");
            })
                    .thenAccept(System.out::println)
                    // >>> [Hello, World]
                    .toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> sadd = reactiveCommands.sadd("myset", "Hello").doOnNext(r -> {
                System.out.println(r); // >>> 1
            }).flatMap(r -> reactiveCommands.sadd("myset", "World")).doOnNext(r -> {
                System.out.println(r); // >>> 1
            }).flatMap(r -> reactiveCommands.sadd("myset", "World")).doOnNext(r -> {
                System.out.println(r); // >>> 0
            }).flatMap(r -> reactiveCommands.smembers("myset").collectList()).doOnNext(r -> {
                System.out.println(r); // >>> [Hello, World]
            }).then();
```

##### Java (Synchronous - Jedis)

```java
        long sAddResult1 = jedis.sadd("myset", "Hello");
        System.out.println(sAddResult1); // >>> 1

        long sAddResult2 = jedis.sadd("myset", "World");
        System.out.println(sAddResult2); // >>> 1

        long sAddResult3 = jedis.sadd("myset", "World");
        System.out.println(sAddResult3); // >>> 0

        Set<String> sAddResult4 = jedis.smembers("myset");
        System.out.println(sAddResult4.stream().sorted().collect(toList()));
        // >>> [Hello, World]
```

##### JavaScript (Node.js)

```javascript
const res1 = await client.sAdd('myset', ['Hello', 'World']);
console.log(res1);  // 2

const res2 = await client.sAdd('myset', ['World']);
console.log(res2);  // 0

const res3 = await client.sMembers('myset')
console.log(res3);  // ['Hello', 'World']

```

##### JavaScript (Node.js)

```javascript
const res1 = await redis.sadd('myset', 'Hello', 'World');
console.log(res1); // >>> 2

const res2 = await redis.sadd('myset', 'World');
console.log(res2); // >>> 0

const res3 = await redis.smembers('myset');
console.log(res3); // >>> ['Hello', 'World']
```

##### JavaScript (Node.js)

```javascript
const res1 = await client.sAdd('myset', ['Hello', 'World']);
console.log(res1);  // 2

const res2 = await client.sAdd('myset', ['World']);
console.log(res2);  // 0

const res3 = await client.sMembers('myset')
console.log(res3);  // ['Hello', 'World']

```

##### JavaScript (Node.js)

```javascript
const res1 = await redis.sadd('myset', 'Hello', 'World');
console.log(res1); // >>> 2

const res2 = await redis.sadd('myset', 'World');
console.log(res2); // >>> 0

const res3 = await redis.smembers('myset');
console.log(res3); // >>> ['Hello', 'World']
```

##### PHP

```php
        $res1 = $r->sadd('myset', ['Hello', 'World']);
        echo $res1 . PHP_EOL;
        // >>> 2

        $res2 = $r->sadd('myset', ['World']);
        echo $res2 . PHP_EOL;
        // >>> 0

        $res3 = $r->smembers('myset');
        sort($res3);
        echo json_encode($res3) . PHP_EOL;
        // >>> ["Hello","World"]
```

##### Python

```python
res1 = r.sadd("myset", "Hello", "World")
print(res1)  # >>> 2

res2 = r.sadd("myset", "World")
print(res2)  # >>> 0

res3 = r.smembers("myset")
print(res3)  # >>> {'Hello', 'World'}

```

##### Ruby

```ruby
res1 = r.sadd('myset', ['Hello', 'World'])
puts res1 # >>> 2

res2 = r.sadd('myset', ['World'])
puts res2 # >>> 0

res3 = r.smembers('myset')
puts res3.inspect # >>> ["Hello", "World"]
```

##### Rust (Asynchronous)

```rust
        if let Ok(res1) = r.sadd("myset", &["Hello", "World"]).await {
            let res1: i32 = res1;
            println!("{res1}"); // >>> 2
        }

        if let Ok(res2) = r.sadd("myset", "World").await {
            let res2: i32 = res2;
            println!("{res2}"); // >>> 0
        }

        if let Ok(res3) = r.smembers("myset").await {
            let res3: HashSet<String> = res3;
            println!("{res3:?}"); // >>> {"Hello", "World"}
        }
```

##### Rust (Synchronous)

```rust
        if let Ok(res1) = r.sadd("myset", &["Hello", "World"]) {
            let res1: i32 = res1;
            println!("{res1}"); // >>> 2
        }

        if let Ok(res2) = r.sadd("myset", "World") {
            let res2: i32 = res2;
            println!("{res2}"); // >>> 0
        }

        if let Ok(res3) = r.smembers("myset") {
            let res3: HashSet<String> = res3;
            println!("{res3:?}"); // >>> {"Hello", "World"}
        }
```



## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Integer reply](../../develop/reference/protocol-spec#integers): the number of elements that were added to the set, not including all the elements already present in the set.

**RESP3:**

[Integer reply](../../develop/reference/protocol-spec#integers): the number of elements that were added to the set, not including all the elements already present in the set.



