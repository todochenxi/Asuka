# LPUSH

```json metadata
{
  "schema_version": 2,
  "title": "LPUSH",
  "description": "Prepends one or more elements to a list. Creates the key if it doesn't exist.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"},{"display_text":"element","multiple":true,"name":"element","type":"string"}],
  "syntax_fmt": "LPUSH key element [element ...]",
  "complexity": "O(1) for each element added, so O(N) to add N elements when the command is called with multiple arguments.",
  "group": "list",
  "command_flags": ["write","denyoom","fast"],
  "acl_categories": ["@write","@list","@fast"],
  "since": "1.0.0",
  "arity": -3,
  "key_specs": [{"RW":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"},"insert":true}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_list-steplpush","commands":[{"acl_categories":["@write","@list","@fast"],"complexity":"O(1)","name":"LPUSH"},{"acl_categories":["@read","@list","@slow"],"complexity":"O(S+N)","name":"LRANGE"}],"description":"Foundational: Add one or more elements to the head of a list using LPUSH (creates list if needed, returns new list length)","difficulty":"beginner","id":"lpush","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_list-steplpush"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_list-steplpush"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_list-steplpush"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_list-steplpush"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_list-steplpush"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_list-steplpush"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_list-steplpush"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_list-steplpush"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_list-steplpush"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_list-steplpush"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_list-steplpush"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_list-steplpush"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_list-steplpush"}]}]
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

Insert all the specified values at the head of the list stored at `key`.
If `key` does not exist, it is created as empty list before performing the push
operations.
When `key` holds a value that is not a list, an error is returned.

It is possible to push multiple elements using a single command call just
specifying multiple arguments at the end of the command.
Elements are inserted one after the other to the head of the list, from the
leftmost element to the rightmost element.
So for instance the command `LPUSH mylist a b c` will result into a list
containing `c` as first element, `b` as second element and `a` as third element.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key that holds the list.

</details>

<details open><summary><code>element [element ...]</code></summary>

One or more values to prepend to the list.

</details>

## Examples
Foundational: Add one or more elements to the head of a list using LPUSH (creates list if needed, returns new list length)

**Difficulty:** Beginner

**Commands:** LPUSH, LRANGE

**Complexity:**
- LPUSH: O(1)
- LRANGE: O(S+N)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
redis> LPUSH mylist "world"
(integer) 1
redis> LPUSH mylist "hello"
(integer) 2
redis> LRANGE mylist 0 -1
1) "hello"
2) "world"
```

##### C#

```csharp
        long lPushResult1 = db.ListLeftPush("mylist", "World");
        Console.WriteLine(lPushResult1); // >>> 1

        long lPushResult2 = db.ListLeftPush("mylist", "Hello");
        Console.WriteLine(lPushResult2); // >>> 2

        RedisValue[] lPushResult3 = db.ListRange("mylist", 0, -1);
        Console.WriteLine($"[{string.Join(", ", lPushResult3)}]");
        // >>> [Hello, World]
```

##### Go

```go
	lPushResult1, err := rdb.LPush(ctx, "mylist", "World").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(lPushResult1) // >>> 1

	lPushResult2, err := rdb.LPush(ctx, "mylist", "Hello").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(lPushResult2) // >>> 2

	lRangeResult, err := rdb.LRange(ctx, "mylist", 0, -1).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(lRangeResult) // >>> [Hello World]
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> lpush = asyncCommands.lpush("mylist", "world").thenCompose(res1 -> {
                System.out.println(res1); // >>> 1

                return asyncCommands.lpush("mylist", "hello");
            }).thenCompose(res2 -> {
                System.out.println(res2); // >>> 2

                return asyncCommands.lrange("mylist", 0, -1);
            })
                    .thenAccept(res3 -> System.out.println(res3)) // >>> [hello, world]
                    .toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> lpush = reactiveCommands.lpush("mylist", "world").doOnNext(res1 -> {
                System.out.println(res1); // >>> 1
            }).flatMap(res1 -> reactiveCommands.lpush("mylist", "hello")).doOnNext(res2 -> {
                System.out.println(res2); // >>> 2
            }).flatMap(res2 -> reactiveCommands.lrange("mylist", 0, -1).collectList()).doOnNext(res3 -> {
                System.out.println(res3); // >>> [hello, world]
            }).then();
```

##### Java (Synchronous - Jedis)

```java
        long lPushResult1 = jedis.lpush("mylist", "World");
        System.out.println(lPushResult1); // >>> 1

        long lPushResult2 = jedis.lpush("mylist", "Hello");
        System.out.println(lPushResult2); // >>> 2

        List<String> lPushResult3 = jedis.lrange("mylist", 0, -1);
        System.out.println(lPushResult3);
        // >>> [Hello, World]
```

##### JavaScript (Node.js)

```javascript
const res1 = await client.lPush('mylist', 'world');
console.log(res1); // 1

const res2 = await client.lPush('mylist', 'hello');
console.log(res2); // 2

const res3 = await client.lRange('mylist', 0, -1);
console.log(res3); // [ 'hello', 'world' ]

```

##### JavaScript (Node.js)

```javascript
const res1 = await redis.lpush('mylist', 'world');
console.log(res1); // >>> 1

const res2 = await redis.lpush('mylist', 'hello');
console.log(res2); // >>> 2

const res3 = await redis.lrange('mylist', 0, -1);
console.log(res3); // >>> ['hello', 'world']
```

##### JavaScript (Node.js)

```javascript
const res1 = await client.lPush('mylist', 'world');
console.log(res1); // 1

const res2 = await client.lPush('mylist', 'hello');
console.log(res2); // 2

const res3 = await client.lRange('mylist', 0, -1);
console.log(res3); // [ 'hello', 'world' ]

```

##### JavaScript (Node.js)

```javascript
const res1 = await redis.lpush('mylist', 'world');
console.log(res1); // >>> 1

const res2 = await redis.lpush('mylist', 'hello');
console.log(res2); // >>> 2

const res3 = await redis.lrange('mylist', 0, -1);
console.log(res3); // >>> ['hello', 'world']
```

##### PHP

```php
        $res1 = $r->lpush('mylist', 'world');
        echo $res1 . PHP_EOL;
        // >>> 1

        $res2 = $r->lpush('mylist', 'hello');
        echo $res2 . PHP_EOL;
        // >>> 2

        $res3 = $r->lrange('mylist', 0, -1);
        echo json_encode($res3) . PHP_EOL;
        // >>> ["hello","world"]
```

##### Python

```python
res1 = r.lpush("mylist", "world")
print(res1) # >>> 1

res2 = r.lpush("mylist", "hello")
print(res2) # >>> 2

res3 = r.lrange("mylist", 0, -1)
print(res3)  # >>> [ "hello", "world" ]

```

##### Ruby

```ruby
res1 = r.lpush('mylist', 'world')
puts res1 # >>> 1

res2 = r.lpush('mylist', 'hello')
puts res2 # >>> 2

res3 = r.lrange('mylist', 0, -1)
puts res3.inspect # >>> ["hello", "world"]
```

##### Rust (Asynchronous)

```rust
        if let Ok(res1) = r.lpush("mylist", "world").await {
            let res1: i32 = res1;
            println!("{res1}"); // >>> 1
        }

        if let Ok(res2) = r.lpush("mylist", "hello").await {
            let res2: i32 = res2;
            println!("{res2}"); // >>> 2
        }

        if let Ok(res3) = r.lrange("mylist", 0, -1).await {
            let res3: Vec<String> = res3;
            println!("{res3:?}"); // >>> ["hello", "world"]
        }
```

##### Rust (Synchronous)

```rust
        if let Ok(res1) = r.lpush("mylist", "world") {
            let res1: i32 = res1;
            println!("{res1}"); // >>> 1
        }

        if let Ok(res2) = r.lpush("mylist", "hello") {
            let res2: i32 = res2;
            println!("{res2}"); // >>> 2
        }

        if let Ok(res3) = r.lrange("mylist", 0, -1) {
            let res3: Vec<String> = res3;
            println!("{res3:?}"); // >>> ["hello", "world"]
        }
```



## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Integer reply](../../develop/reference/protocol-spec#integers): the length of the list after the push operation.

**RESP3:**

[Integer reply](../../develop/reference/protocol-spec#integers): the length of the list after the push operation.



