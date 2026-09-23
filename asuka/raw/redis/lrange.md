# LRANGE

```json metadata
{
  "schema_version": 2,
  "title": "LRANGE",
  "description": "Returns a range of elements from a list.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"},{"display_text":"start","name":"start","type":"integer"},{"display_text":"stop","name":"stop","type":"integer"}],
  "syntax_fmt": "LRANGE key start stop",
  "complexity": "O(S+N) where S is the distance of start offset from HEAD for small lists, from nearest end (HEAD or TAIL) for large lists; and N is the number of elements in the specified range.",
  "group": "list",
  "command_flags": ["readonly"],
  "acl_categories": ["@read","@list","@slow"],
  "since": "1.0.0",
  "arity": 4,
  "key_specs": [{"RO":true,"access":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"}}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"children":[{"id":"consistency-with-range-functions-in-various-programming-languages","title":"Consistency with range functions in various programming languages"},{"id":"out-of-range-indexes","title":"Out-of-range indexes"}],"id":"details","title":"Details"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_list-steplrange","commands":[{"acl_categories":["@write","@list","@fast"],"complexity":"O(1)","name":"RPUSH"},{"acl_categories":["@read","@list","@slow"],"complexity":"O(S+N)","name":"LRANGE"}],"description":"Foundational: Retrieve a range of elements from a list using LRANGE with start and stop indexes (supports negative indexes, inclusive range)","difficulty":"beginner","id":"lrange","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_list-steplrange"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_list-steplrange"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_list-steplrange"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_list-steplrange"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_list-steplrange"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_list-steplrange"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_list-steplrange"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_list-steplrange"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_list-steplrange"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_list-steplrange"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_list-steplrange"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_list-steplrange"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_list-steplrange"}]}]
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

Returns the specified elements of the list stored at `key`.
The offsets `start` and `stop` are zero-based indexes, with `0` being the first
element of the list (the head of the list), `1` being the next element and so
on.

These offsets can also be negative numbers indicating offsets starting at the
end of the list.
For example, `-1` is the last element of the list, `-2` the penultimate, and so
on.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key that holds the list.

</details>

<details open><summary><code>start</code></summary>

The zero-based start index. Negative indexes count from the tail.

</details>

<details open><summary><code>stop</code></summary>

The zero-based stop index (inclusive). Negative indexes count from the tail.

</details>

## Examples

Foundational: Retrieve a range of elements from a list using LRANGE with start and stop indexes (supports negative indexes, inclusive range)

**Difficulty:** Beginner

**Commands:** RPUSH, LRANGE

**Complexity:**
- RPUSH: O(1)
- LRANGE: O(S+N)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
redis> RPUSH mylist "one"
(integer) 1
redis> RPUSH mylist "two"
(integer) 2
redis> RPUSH mylist "three"
(integer) 3
redis> LRANGE mylist 0 0
1) "one"
redis> LRANGE mylist -3 2
1) "one"
2) "two"
3) "three"
redis> LRANGE mylist -100 100
1) "one"
2) "two"
3) "three"
redis> LRANGE mylist 5 10
(empty array)
```

##### C#

```csharp
        long lRangeResult1 = db.ListRightPush("mylist", ["one", "two", "three"]);
        Console.WriteLine(lRangeResult1); // >>> 3

        RedisValue[] lRangeResult2 = db.ListRange("mylist", 0, 0);
        Console.WriteLine($"[{string.Join(", ", lRangeResult2)}]");
        // >>> [one]

        RedisValue[] lRangeResult3 = db.ListRange("mylist", -3, 2);
        Console.WriteLine($"[{string.Join(", ", lRangeResult3)}]");
        // >>> [one, two, three]

        RedisValue[] lRangeResult4 = db.ListRange("mylist", -100, 100);
        Console.WriteLine($"[{string.Join(", ", lRangeResult4)}]");
        // >>> [one, two, three]

        RedisValue[] lRangeResult5 = db.ListRange("mylist", 5, 10);
        Console.WriteLine($"[{string.Join(", ", lRangeResult5)}]");
        // >>> []
```

##### Go

```go
	RPushResult, err := rdb.RPush(ctx, "mylist",
		"one", "two", "three",
	).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(RPushResult) // >>> 3

	lRangeResult1, err := rdb.LRange(ctx, "mylist", 0, 0).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(lRangeResult1) // >>> [one]

	lRangeResult2, err := rdb.LRange(ctx, "mylist", -3, 2).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(lRangeResult2) // >>> [one two three]

	lRangeResult3, err := rdb.LRange(ctx, "mylist", -100, 100).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(lRangeResult3) // >>> [one two three]

	lRangeResult4, err := rdb.LRange(ctx, "mylist", 5, 10).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(lRangeResult4) // >>> []
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> lrange = asyncCommands.rpush("mylist", "one").thenCompose(res4 -> {
                System.out.println(res4); // >>> 1

                return asyncCommands.rpush("mylist", "two");
            }).thenCompose(res5 -> {
                System.out.println(res5); // >>> 2

                return asyncCommands.rpush("mylist", "three");
            }).thenCompose(res6 -> {
                System.out.println(res6); // >>> 3

                return asyncCommands.lrange("mylist", 0, 0);
            }).thenCompose(res7 -> {
                System.out.println(res7); // >>> [one]

                return asyncCommands.lrange("mylist", -3, 2);
            }).thenCompose(res8 -> {
                System.out.println(res8); // >>> [one, two, three]

                return asyncCommands.lrange("mylist", -100, 100);
            }).thenCompose(res9 -> {
                System.out.println(res9); // >>> [one, two, three]

                return asyncCommands.lrange("mylist", 5, 10);
            })
                    .thenAccept(res10 -> System.out.println(res10)) // >>> []
                    .toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> lrange = reactiveCommands.rpush("mylist", "one").doOnNext(res4 -> {
                System.out.println(res4); // >>> 1
            }).flatMap(res4 -> reactiveCommands.rpush("mylist", "two")).doOnNext(res5 -> {
                System.out.println(res5); // >>> 2
            }).flatMap(res5 -> reactiveCommands.rpush("mylist", "three")).doOnNext(res6 -> {
                System.out.println(res6); // >>> 3
            }).flatMap(res6 -> reactiveCommands.lrange("mylist", 0, 0).collectList()).doOnNext(res7 -> {
                System.out.println(res7); // >>> [one]
            }).flatMap(res7 -> reactiveCommands.lrange("mylist", -3, 2).collectList()).doOnNext(res8 -> {
                System.out.println(res8); // >>> [one, two, three]
            }).flatMap(res8 -> reactiveCommands.lrange("mylist", -100, 100).collectList()).doOnNext(res9 -> {
                System.out.println(res9); // >>> [one, two, three]
            }).flatMap(res9 -> reactiveCommands.lrange("mylist", 5, 10).collectList()).doOnNext(res10 -> {
                System.out.println(res10); // >>> []
            }).then();
```

##### Java (Synchronous - Jedis)

```java
        long lRangeResult1 = jedis.rpush("mylist", "one", "two", "three");
        System.out.println(lRangeResult1); // >>> 3

        List<String> lRangeResult2 = jedis.lrange("mylist", 0, 0);
        System.out.println(lRangeResult2); // >>> [one]

        List<String> lRangeResult3 = jedis.lrange("mylist", -3, 2);
        System.out.println(lRangeResult3); // >>> [one, two, three]

        List<String> lRangeResult4 = jedis.lrange("mylist", -100, 100);
        System.out.println(lRangeResult4); // >>> [one, two, three]

        List<String> lRangeResult5 = jedis.lrange("mylist", 5, 10);
        System.out.println(lRangeResult5); // >>> []
```

##### JavaScript (Node.js)

```javascript
const res4 = await client.rPush('mylist', 'one');
console.log(res4); // 1

const res5 = await client.rPush('mylist', 'two');
console.log(res5); // 2

const res6 = await client.rPush('mylist', 'three');
console.log(res6); // 3

const res7 = await client.lRange('mylist', 0, 0);
console.log(res7); // [ 'one' ]

const res8 = await client.lRange('mylist', -3, 2);
console.log(res8); // [ 'one', 'two', 'three' ]

const res9 = await client.lRange('mylist', -100, 100);
console.log(res9); // [ 'one', 'two', 'three' ]

const res10 = await client.lRange('mylist', 5, 10);
console.log(res10); // []

```

##### JavaScript (Node.js)

```javascript
const res4 = await redis.rpush('mylist', 'one');
console.log(res4); // >>> 1

const res5 = await redis.rpush('mylist', 'two');
console.log(res5); // >>> 2

const res6 = await redis.rpush('mylist', 'three');
console.log(res6); // >>> 3

const res7 = await redis.lrange('mylist', 0, 0);
console.log(res7); // >>> ['one']

const res8 = await redis.lrange('mylist', -3, 2);
console.log(res8); // >>> ['one', 'two', 'three']

const res9 = await redis.lrange('mylist', -100, 100);
console.log(res9); // >>> ['one', 'two', 'three']

const res10 = await redis.lrange('mylist', 5, 10);
console.log(res10); // >>> []
```

##### JavaScript (Node.js)

```javascript
const res4 = await client.rPush('mylist', 'one');
console.log(res4); // 1

const res5 = await client.rPush('mylist', 'two');
console.log(res5); // 2

const res6 = await client.rPush('mylist', 'three');
console.log(res6); // 3

const res7 = await client.lRange('mylist', 0, 0);
console.log(res7); // [ 'one' ]

const res8 = await client.lRange('mylist', -3, 2);
console.log(res8); // [ 'one', 'two', 'three' ]

const res9 = await client.lRange('mylist', -100, 100);
console.log(res9); // [ 'one', 'two', 'three' ]

const res10 = await client.lRange('mylist', 5, 10);
console.log(res10); // []

```

##### JavaScript (Node.js)

```javascript
const res4 = await redis.rpush('mylist', 'one');
console.log(res4); // >>> 1

const res5 = await redis.rpush('mylist', 'two');
console.log(res5); // >>> 2

const res6 = await redis.rpush('mylist', 'three');
console.log(res6); // >>> 3

const res7 = await redis.lrange('mylist', 0, 0);
console.log(res7); // >>> ['one']

const res8 = await redis.lrange('mylist', -3, 2);
console.log(res8); // >>> ['one', 'two', 'three']

const res9 = await redis.lrange('mylist', -100, 100);
console.log(res9); // >>> ['one', 'two', 'three']

const res10 = await redis.lrange('mylist', 5, 10);
console.log(res10); // >>> []
```

##### PHP

```php
        $res4 = $r->rpush('mylist', 'one');
        echo $res4 . PHP_EOL;
        // >>> 1

        $res5 = $r->rpush('mylist', 'two');
        echo $res5 . PHP_EOL;
        // >>> 2

        $res6 = $r->rpush('mylist', 'three');
        echo $res6 . PHP_EOL;
        // >>> 3

        $res7 = $r->lrange('mylist', 0, 0);
        echo json_encode($res7) . PHP_EOL;
        // >>> ["one"]

        $res8 = $r->lrange('mylist', -3, 2);
        echo json_encode($res8) . PHP_EOL;
        // >>> ["one","two","three"]

        $res9 = $r->lrange('mylist', -100, 100);
        echo json_encode($res9) . PHP_EOL;
        // >>> ["one","two","three"]

        $res10 = $r->lrange('mylist', 5, 10);
        echo json_encode($res10) . PHP_EOL;
        // >>> []
```

##### Python

```python
res4 = r.rpush("mylist", "one");
print(res4) # >>> 1

res5 = r.rpush("mylist", "two")
print(res5) # >>> 2

res6 = r.rpush("mylist", "three")
print(res6) # >>> 3

res7 = r.lrange('mylist', 0, 0)
print(res7) # >>> [ 'one' ]

res8 = r.lrange('mylist', -3, 2)
print(res8) # >>> [ 'one', 'two', 'three' ]

res9 = r.lrange('mylist', -100, 100)
print(res9) # >>> [ 'one', 'two', 'three' ]

res10 = r.lrange('mylist', 5, 10)
print(res10) # >>> []

```

##### Ruby

```ruby
res4 = r.rpush('mylist', 'one')
puts res4 # >>> 1

res5 = r.rpush('mylist', 'two')
puts res5 # >>> 2

res6 = r.rpush('mylist', 'three')
puts res6 # >>> 3

res7 = r.lrange('mylist', 0, 0)
puts res7.inspect # >>> ["one"]

res8 = r.lrange('mylist', -3, 2)
puts res8.inspect # >>> ["one", "two", "three"]

res9 = r.lrange('mylist', -100, 100)
puts res9.inspect # >>> ["one", "two", "three"]

res10 = r.lrange('mylist', 5, 10)
puts res10.inspect # >>> []
```

##### Rust (Asynchronous)

```rust
        let _: Result<i32, _> = r.rpush("mylist", "one").await;
        let _: Result<i32, _> = r.rpush("mylist", "two").await;
        let _: Result<i32, _> = r.rpush("mylist", "three").await;

        if let Ok(res7) = r.lrange("mylist", 0, 0).await {
            let res7: Vec<String> = res7;
            println!("{res7:?}"); // >>> ["one"]
        }

        if let Ok(res8) = r.lrange("mylist", -3, 2).await {
            let res8: Vec<String> = res8;
            println!("{res8:?}"); // >>> ["one", "two", "three"]
        }

        if let Ok(res9) = r.lrange("mylist", -100, 100).await {
            let res9: Vec<String> = res9;
            println!("{res9:?}"); // >>> ["one", "two", "three"]
        }

        if let Ok(res10) = r.lrange("mylist", 5, 10).await {
            let res10: Vec<String> = res10;
            println!("{res10:?}"); // >>> []
        }
```

##### Rust (Synchronous)

```rust
        let _: Result<i32, _> = r.rpush("mylist", "one");
        let _: Result<i32, _> = r.rpush("mylist", "two");
        let _: Result<i32, _> = r.rpush("mylist", "three");

        if let Ok(res7) = r.lrange("mylist", 0, 0) {
            let res7: Vec<String> = res7;
            println!("{res7:?}"); // >>> ["one"]
        }

        if let Ok(res8) = r.lrange("mylist", -3, 2) {
            let res8: Vec<String> = res8;
            println!("{res8:?}"); // >>> ["one", "two", "three"]
        }

        if let Ok(res9) = r.lrange("mylist", -100, 100) {
            let res9: Vec<String> = res9;
            println!("{res9:?}"); // >>> ["one", "two", "three"]
        }

        if let Ok(res10) = r.lrange("mylist", 5, 10) {
            let res10: Vec<String> = res10;
            println!("{res10:?}"); // >>> []
        }
```



## Details

### Consistency with range functions in various programming languages

Note that if you have a list of numbers from 0 to 100, `LRANGE list 0 10` will
return 11 elements, that is, the rightmost item is included.
This may or may not be consistent with the behavior of range-related functions
in your programming language of choice (think Ruby's `Range.new`, `Array#slice`
or Python's `range()` function).

### Out-of-range indexes

Out of range indexes will not produce an error.
If `start` is larger than the end of the list, an empty list is returned.
If `stop` is larger than the actual end of the list, Redis will treat it like
the last element of the list.

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Array reply](../../develop/reference/protocol-spec#arrays): a list of elements in the specified range, or an empty array if the key doesn't exist.

**RESP3:**

[Array reply](../../develop/reference/protocol-spec#arrays): a list of elements in the specified range, or an empty array if the key doesn't exist.



