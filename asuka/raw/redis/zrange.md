# ZRANGE

```json metadata
{
  "schema_version": 2,
  "title": "ZRANGE",
  "description": "Returns members in a sorted set within a range of indexes.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"},{"display_text":"start","name":"start","type":"string"},{"display_text":"stop","name":"stop","type":"string"},{"arguments":[{"display_text":"byscore","name":"byscore","token":"BYSCORE","type":"pure-token"},{"display_text":"bylex","name":"bylex","token":"BYLEX","type":"pure-token"}],"name":"sortby","optional":true,"since":"6.2.0","type":"oneof"},{"display_text":"rev","name":"rev","optional":true,"since":"6.2.0","token":"REV","type":"pure-token"},{"arguments":[{"display_text":"offset","name":"offset","type":"integer"},{"display_text":"count","name":"count","type":"integer"}],"name":"limit","optional":true,"since":"6.2.0","token":"LIMIT","type":"block"},{"display_text":"withscores","name":"withscores","optional":true,"token":"WITHSCORES","type":"pure-token"}],
  "syntax_fmt": "ZRANGE key start stop [BYSCORE | BYLEX] [REV] [LIMIT offset count]\n  [WITHSCORES]",
  "complexity": "O(log(N)+M) with N being the number of elements in the sorted set and M the number of elements returned.",
  "group": "sorted-set",
  "command_flags": ["readonly"],
  "acl_categories": ["@read","@sortedset","@slow"],
  "since": "1.2.0",
  "arity": -4,
  "key_specs": [{"RO":true,"access":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"}}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"optional-arguments","title":"Optional arguments"},{"id":"examples","title":"Examples"},{"children":[{"id":"common-behavior-and-options","title":"Common behavior and options"},{"id":"index-ranges","title":"Index ranges"},{"id":"score-ranges","title":"Score ranges"},{"id":"reverse-ranges","title":"Reverse ranges"},{"id":"lexicographical-ranges","title":"Lexicographical ranges"}],"id":"details","title":"Details"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_sorted_set-stepzrange1","commands":[{"acl_categories":["@write","@sortedset","@fast"],"complexity":"O(log(N)","name":"ZADD"},{"acl_categories":["@read","@sortedset","@slow"],"complexity":"O(log(N)","name":"ZRANGE"}],"description":"Foundational: Retrieve a range of members from a sorted set by index using ZRANGE (supports negative indexes, inclusive range)","difficulty":"beginner","id":"zrange1","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_sorted_set-stepzrange1"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_sorted_set-stepzrange1"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_sorted_set-stepzrange1"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_sorted_set-stepzrange1"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_sorted_set-stepzrange1"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_sorted_set-stepzrange1"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_sorted_set-stepzrange1"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_sorted_set-stepzrange1"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_sorted_set-stepzrange1"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_sorted_set-stepzrange1"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_sorted_set-stepzrange1"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_sorted_set-stepzrange1"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_sorted_set-stepzrange1"}]},{"codetabsId":"cmds_sorted_set-stepzrange2","commands":[{"acl_categories":["@keyspace","@write","@slow"],"complexity":"O(N)","name":"DEL"},{"acl_categories":["@write","@sortedset","@fast"],"complexity":"O(log(N)","name":"ZADD"},{"acl_categories":["@read","@sortedset","@slow"],"complexity":"O(log(N)","name":"ZRANGE"}],"description":"Return scores with members: Retrieve members with their scores from a sorted set using ZRANGE with WITHSCORES option","difficulty":"intermediate","id":"zrange2","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_sorted_set-stepzrange2"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_sorted_set-stepzrange2"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_sorted_set-stepzrange2"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_sorted_set-stepzrange2"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_sorted_set-stepzrange2"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_sorted_set-stepzrange2"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_sorted_set-stepzrange2"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_sorted_set-stepzrange2"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_sorted_set-stepzrange2"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_sorted_set-stepzrange2"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_sorted_set-stepzrange2"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_sorted_set-stepzrange2"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_sorted_set-stepzrange2"}]},{"codetabsId":"cmds_sorted_set-stepzrange3","commands":[{"acl_categories":["@keyspace","@write","@slow"],"complexity":"O(N)","name":"DEL"},{"acl_categories":["@write","@sortedset","@fast"],"complexity":"O(log(N)","name":"ZADD"},{"acl_categories":["@read","@sortedset","@slow"],"complexity":"O(log(N)","name":"ZRANGE"}],"description":"Query by score: Query a sorted set by score range using ZRANGE with BYSCORE and LIMIT options (supports exclusive ranges and pagination)","difficulty":"intermediate","id":"zrange3","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_sorted_set-stepzrange3"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_sorted_set-stepzrange3"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_sorted_set-stepzrange3"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_sorted_set-stepzrange3"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_sorted_set-stepzrange3"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_sorted_set-stepzrange3"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_sorted_set-stepzrange3"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_sorted_set-stepzrange3"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_sorted_set-stepzrange3"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_sorted_set-stepzrange3"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_sorted_set-stepzrange3"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_sorted_set-stepzrange3"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_sorted_set-stepzrange3"}]}]
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

Returns the specified range of elements in the sorted set stored at `<key>`.

`ZRANGE` can perform different types of range queries: by index (rank), by the score, or by lexicographical order.

Starting with Redis 6.2.0, this command can replace the following commands: [`ZREVRANGE`](https://redis.io/docs/latest/commands/zrevrange), [`ZRANGEBYSCORE`](https://redis.io/docs/latest/commands/zrangebyscore), [`ZREVRANGEBYSCORE`](https://redis.io/docs/latest/commands/zrevrangebyscore), [`ZRANGEBYLEX`](https://redis.io/docs/latest/commands/zrangebylex) and [`ZREVRANGEBYLEX`](https://redis.io/docs/latest/commands/zrevrangebylex).

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key that holds the sorted set.

</details>

<details open><summary><code>start</code></summary>

The start of the range. By default a zero-based index (negatives count from the end); a score bound with `BYSCORE`; or a lexicographical bound with `BYLEX`.

</details>

<details open><summary><code>stop</code></summary>

The end of the range, inclusive. Interpreted the same way as `start`.

</details>

## Optional arguments

<details open><summary><code>BYSCORE | BYLEX</code></summary>

Interpret the range as a score range (`BYSCORE`) or a lexicographical range (`BYLEX`) instead of an index range.

</details>

<details open><summary><code>REV</code></summary>

Reverse the ordering so results are returned from high to low.

</details>

<details open><summary><code>LIMIT offset count</code></summary>

Skip `offset` matching members and return up to `count` of them. Requires `BYSCORE` or `BYLEX`. A negative `count` returns all remaining members.

</details>

<details open><summary><code>WITHSCORES</code></summary>

Also return the score of each member.

</details>

## Examples

Foundational: Retrieve a range of members from a sorted set by index using ZRANGE (supports negative indexes, inclusive range)

**Difficulty:** Beginner

**Commands:** ZADD, ZRANGE

**Complexity:**
- ZADD: O(log(N)
- ZRANGE: O(log(N)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> ZADD myzset 1 "one" 2 "two" 3 "three"
(integer) 3
> ZRANGE myzset 0 -1
1) "one"
2) "two"
3) "three"
> ZRANGE myzset 2 3
1) "three"
> ZRANGE myzset -2 -1
1) "two"
2) "three"
```

##### C#

```csharp
        long zRangeResult1 = db.SortedSetAdd(
            "myzset",
            [
                new("one", 1),
                new("two", 2),
                new("three", 3)
            ]
        );
        Console.WriteLine(zRangeResult1);   // >>> 3

        RedisValue[] zRangeResult2 = db.SortedSetRangeByRank("myzset", 0, -1);
        Console.WriteLine(string.Join(", ", zRangeResult2));
        // >>> one, two, three

        RedisValue[] zRangeResult3 = db.SortedSetRangeByRank("myzset", 2, 3);
        Console.WriteLine(string.Join(", ", zRangeResult3));
        // >>> three

        RedisValue[] zRangeResult4 = db.SortedSetRangeByRank("myzset", -2, -1);
        Console.WriteLine(string.Join(", ", zRangeResult4));
        // >>> two, three
```

##### Go

```go
	zrangeResult1, err := rdb.ZAdd(ctx, "myzset",
		redis.Z{Member: "one", Score: 1},
		redis.Z{Member: "two", Score: 2},
		redis.Z{Member: "three", Score: 3},
	).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zrangeResult1) // >>> 3

	zrangeResult2, err := rdb.ZRange(ctx, "myzset", 0, -1).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zrangeResult2) // >>> [one two three]

	zrangeResult3, err := rdb.ZRange(ctx, "myzset", 2, 3).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zrangeResult3) // >>> [three]

	zrangeResult4, err := rdb.ZRange(ctx, "myzset", -2, -1).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zrangeResult4) // >>> [two three]
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> zRange1Example = asyncCommands.zadd("myzset",
                    ScoredValue.just(1d, "one"), ScoredValue.just(2d, "two"), ScoredValue.just(3d, "three"))
                    .thenCompose(res5 -> {
                        System.out.println(res5); // >>> 3
                        return asyncCommands.zrange("myzset", 0, -1);
                    }).thenCompose(res6 -> {
                        System.out.println(res6); // >>> [one, two, three]
                        return asyncCommands.zrange("myzset", 2, 3);
                    }).thenCompose(res7 -> {
                        System.out.println(res7); // >>> [three]
                        return asyncCommands.zrange("myzset", -2, -1);
                    }).thenAccept(res8 -> {
                        System.out.println(res8); // >>> [two, three]
                    }).toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> zRange1Example = reactiveCommands.zadd("myzset",
                    ScoredValue.just(1d, "one"), ScoredValue.just(2d, "two"), ScoredValue.just(3d, "three"))
                    .doOnNext(res5 -> {
                        System.out.println(res5); // >>> 3
                    })
                    .then(reactiveCommands.zrange("myzset", 0, -1).collectList())
                    .doOnNext(res6 -> {
                        System.out.println(res6); // >>> [one, two, three]
                    })
                    .then(reactiveCommands.zrange("myzset", 2, 3).collectList())
                    .doOnNext(res7 -> {
                        System.out.println(res7); // >>> [three]
                    })
                    .then(reactiveCommands.zrange("myzset", -2, -1).collectList())
                    .doOnNext(res8 -> {
                        System.out.println(res8); // >>> [two, three]
                    })
                    .then();
```

##### Java (Synchronous - Jedis)

```java
        Map<String, Double> zRangeExampleParams1 = new HashMap<>();
        zRangeExampleParams1.put("one", 1.0);
        zRangeExampleParams1.put("two", 2.0);
        zRangeExampleParams1.put("three", 3.0);
        long zRangeResult1 = jedis.zadd("myzset", zRangeExampleParams1);
        System.out.println(zRangeResult1);  // >>> 3

        List<String> zRangeResult2 = jedis.zrange("myzset", new ZRangeParams(0, -1));
        System.out.println(String.join(", ", zRangeResult2));   // >>> one, two, three

        List<String> zRangeResult3 = jedis.zrange("myzset", new ZRangeParams(2, 3));
        System.out.println(String.join(", ", zRangeResult3));   // >> three

        List<String> zRangeResult4 = jedis.zrange("myzset", new ZRangeParams(-2, -1));
        System.out.println(String.join(", ", zRangeResult4));   // >> two, three
```

##### JavaScript (Node.js)

```javascript
const val5 = await client.zAdd("myzset", [
  { value: 'one', score: 1 },
  { value: 'two', score: 2 },
  { value: 'three', score: 3 }
]);
console.log(val5);
// returns 3

const val6 = await client.zRange('myzset', 0, -1);
console.log(val6);
// returns ['one', 'two', 'three']

const val7 = await client.zRange('myzset', 2, 3);
console.log(val7);
// returns ['three']

const val8 = await client.zRange('myzset', -2, -1);
console.log(val8);
// returns ['two', 'three']
```

##### JavaScript (Node.js)

```javascript
const res5 = await redis.zadd('myzset', 1, 'one', 2, 'two', 3, 'three');
console.log(res5); // >>> 3

const res6 = await redis.zrange('myzset', 0, -1);
console.log(res6); // >>> ['one', 'two', 'three']

const res7 = await redis.zrange('myzset', 2, 3);
console.log(res7); // >>> ['three']

const res8 = await redis.zrange('myzset', -2, -1);
console.log(res8); // >>> ['two', 'three']
```

##### JavaScript (Node.js)

```javascript
const val5 = await client.zAdd("myzset", [
  { value: 'one', score: 1 },
  { value: 'two', score: 2 },
  { value: 'three', score: 3 }
]);
console.log(val5);
// returns 3

const val6 = await client.zRange('myzset', 0, -1);
console.log(val6);
// returns ['one', 'two', 'three']

const val7 = await client.zRange('myzset', 2, 3);
console.log(val7);
// returns ['three']

const val8 = await client.zRange('myzset', -2, -1);
console.log(val8);
// returns ['two', 'three']
```

##### JavaScript (Node.js)

```javascript
const res5 = await redis.zadd('myzset', 1, 'one', 2, 'two', 3, 'three');
console.log(res5); // >>> 3

const res6 = await redis.zrange('myzset', 0, -1);
console.log(res6); // >>> ['one', 'two', 'three']

const res7 = await redis.zrange('myzset', 2, 3);
console.log(res7); // >>> ['three']

const res8 = await redis.zrange('myzset', -2, -1);
console.log(res8); // >>> ['two', 'three']
```

##### PHP

```php
        $res5 = $r->zadd('myzset', ['one' => 1, 'two' => 2, 'three' => 3]);
        echo $res5 . PHP_EOL; // >>> 3

        $res6 = $r->zrange('myzset', 0, -1);
        echo json_encode($res6) . PHP_EOL; // >>> ["one","two","three"]

        $res7 = $r->zrange('myzset', 2, 3);
        echo json_encode($res7) . PHP_EOL; // >>> ["three"]

        $res8 = $r->zrange('myzset', -2, -1);
        echo json_encode($res8) . PHP_EOL; // >>> ["two","three"]
```

##### Python

```python
res = r.zadd("myzset", {"one": 1, "two":2, "three":3})
print(res)
# >>> 3

res = r.zrange("myzset", 0, -1)
print(res)
# >>> ['one', 'two', 'three']

res = r.zrange("myzset", 2, 3)
print(res)
# >>> ['three']

res = r.zrange("myzset", -2, -1)
print(res)
# >>> ['two', 'three']
```

##### Ruby

```ruby
res5 = r.zadd('myzset', [[1, 'one'], [2, 'two'], [3, 'three']])
puts res5 # >>> 3

res6 = r.zrange('myzset', 0, -1)
puts res6.inspect # >>> ["one", "two", "three"]

res7 = r.zrange('myzset', 2, 3)
puts res7.inspect # >>> ["three"]

res8 = r.zrange('myzset', -2, -1)
puts res8.inspect # >>> ["two", "three"]
```

##### Rust (Asynchronous)

```rust
        if let Ok(res5) = r.zadd_multiple("myzset", &[(1, "one"), (2, "two"), (3, "three")]).await {
            let res5: i32 = res5;
            println!("{res5}"); // >>> 3
        }

        if let Ok(res6) = r.zrange("myzset", 0, -1).await {
            let res6: Vec<String> = res6;
            println!("{res6:?}"); // >>> ["one", "two", "three"]
        }

        if let Ok(res7) = r.zrange("myzset", 2, 3).await {
            let res7: Vec<String> = res7;
            println!("{res7:?}"); // >>> ["three"]
        }

        if let Ok(res8) = r.zrange("myzset", -2, -1).await {
            let res8: Vec<String> = res8;
            println!("{res8:?}"); // >>> ["two", "three"]
        }
```

##### Rust (Synchronous)

```rust
        if let Ok(res5) = r.zadd_multiple("myzset", &[(1, "one"), (2, "two"), (3, "three")]) {
            let res5: i32 = res5;
            println!("{res5}"); // >>> 3
        }

        if let Ok(res6) = r.zrange("myzset", 0, -1) {
            let res6: Vec<String> = res6;
            println!("{res6:?}"); // >>> ["one", "two", "three"]
        }

        if let Ok(res7) = r.zrange("myzset", 2, 3) {
            let res7: Vec<String> = res7;
            println!("{res7:?}"); // >>> ["three"]
        }

        if let Ok(res8) = r.zrange("myzset", -2, -1) {
            let res8: Vec<String> = res8;
            println!("{res8:?}"); // >>> ["two", "three"]
        }
```



The following example using `WITHSCORES` shows how the command returns always an array, but this time, populated with *element_1*, *score_1*, *element_2*, *score_2*, ..., *element_N*, *score_N*.

Return scores with members: Retrieve members with their scores from a sorted set using ZRANGE with WITHSCORES option

**Difficulty:** Intermediate

**Commands:** DEL, ZADD, ZRANGE

**Complexity:**
- DEL: O(N)
- ZADD: O(log(N)
- ZRANGE: O(log(N)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> DEL myzset
(integer) 1
> ZADD myzset 1 "one" 2 "two" 3 "three"
(integer) 3
> ZRANGE myzset 0 1 WITHSCORES
1) "one"
2) "1"
3) "two"
4) "2"
```

##### C#

```csharp
        long zRangeResult5 = db.SortedSetAdd(
            "myzset",
            [
                new("one", 1),
                new("two", 2),
                new("three", 3)
            ]
        );

        SortedSetEntry[] zRangeResult6 = db.SortedSetRangeByRankWithScores("myzset", 0, 1);
        Console.WriteLine($"{string.Join(", ", zRangeResult6.Select(b => $"{b.Element}: {b.Score}"))}");
        // >>> one: 1, two: 2
```

##### Go

```go
	zRangeResult5, err := rdb.ZAdd(ctx, "myzset",
		redis.Z{Member: "one", Score: 1},
		redis.Z{Member: "two", Score: 2},
		redis.Z{Member: "three", Score: 3},
	).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zRangeResult5) // >>> 3

	zRangeResult6, err := rdb.ZRangeWithScores(ctx, "myzset", 0, 1).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zRangeResult6) // >>> [{1 one} {2 two}]
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> zRange2Example = asyncCommands.zadd("myzset",
                    ScoredValue.just(1d, "one"), ScoredValue.just(2d, "two"), ScoredValue.just(3d, "three"))
                    .thenCompose(res9 -> {
                        return asyncCommands.zrangeWithScores("myzset", 0, 1);
                    }).thenAccept(res10 -> {
                        System.out.println(res10);
                        // >>> [ScoredValue[1.000000, one], ScoredValue[2.000000, two]]
                    }).toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> zRange2Example = reactiveCommands.zadd("myzset",
                    ScoredValue.just(1d, "one"), ScoredValue.just(2d, "two"), ScoredValue.just(3d, "three"))
                    .then(reactiveCommands.zrangeWithScores("myzset", 0, 1).collectList())
                    .doOnNext(res9 -> {
                        System.out.println(res9);
                        // >>> [ScoredValue[1.000000, one], ScoredValue[2.000000, two]]
                    })
                    .then();
```

##### Java (Synchronous - Jedis)

```java
        Map<String, Double> zRangeExampleParams2 = new HashMap<>();
        zRangeExampleParams2.put("one", 1.0);
        zRangeExampleParams2.put("two", 2.0);
        zRangeExampleParams2.put("three", 3.0);
        long zRangeResult5 = jedis.zadd("myzset", zRangeExampleParams2);
        System.out.println(zRangeResult5);  // >>> 3

        List<Tuple> zRangeResult6 = jedis.zrangeWithScores("myzset", new ZRangeParams(0, 1));

        for (Tuple item: zRangeResult6) {
            System.out.println("Element: " + item.getElement() + ", Score: " + item.getScore());
        }
        // >>> Element: one, Score: 1.0
        // >>> Element: two, Score: 2.0
```

##### JavaScript (Node.js)

```javascript
const val9 = await client.zAdd("myzset", [
  { value: 'one', score: 1 },
  { value: 'two', score: 2 },
  { value: 'three', score: 3 }
]);
console.log(val9);
// returns 3

const val10 = await client.zRangeWithScores('myzset', 0, 1);
console.log(val10);
// returns [{value: 'one', score: 1}, {value: 'two', score: 2}]
```

##### JavaScript (Node.js)

```javascript
await redis.zadd('myzset', 1, 'one', 2, 'two', 3, 'three');

const res9 = await redis.zrange('myzset', 0, 1, 'WITHSCORES');
console.log(res9); // >>> ['one', '1', 'two', '2']
```

##### JavaScript (Node.js)

```javascript
const val9 = await client.zAdd("myzset", [
  { value: 'one', score: 1 },
  { value: 'two', score: 2 },
  { value: 'three', score: 3 }
]);
console.log(val9);
// returns 3

const val10 = await client.zRangeWithScores('myzset', 0, 1);
console.log(val10);
// returns [{value: 'one', score: 1}, {value: 'two', score: 2}]
```

##### JavaScript (Node.js)

```javascript
await redis.zadd('myzset', 1, 'one', 2, 'two', 3, 'three');

const res9 = await redis.zrange('myzset', 0, 1, 'WITHSCORES');
console.log(res9); // >>> ['one', '1', 'two', '2']
```

##### PHP

```php
        $r->zadd('myzset', ['one' => 1, 'two' => 2, 'three' => 3]);

        $res9 = $r->zrange('myzset', 0, 1, ['withscores' => true]);
        echo json_encode($res9) . PHP_EOL; // >>> {"one":"1","two":"2"}
```

##### Python

```python
res = r.zadd("myzset", {"one": 1, "two":2, "three":3})
res = r.zrange("myzset", 0, 1, withscores=True)
print(res)
# >>> [('one', 1.0), ('two', 2.0)]
```

##### Ruby

```ruby
r.zadd('myzset', [[1, 'one'], [2, 'two'], [3, 'three']])

res9 = r.zrange('myzset', 0, 1, with_scores: true)
puts res9.inspect # >>> [["one", 1.0], ["two", 2.0]]
```

##### Rust (Asynchronous)

```rust
        let _: Result<i32, _> = r.zadd_multiple("myzset", &[(1, "one"), (2, "two"), (3, "three")]).await;

        if let Ok(res9) = r.zrange_withscores("myzset", 0, 1).await {
            let res9: Vec<(String, f64)> = res9;
            println!("{res9:?}"); // >>> [("one", 1.0), ("two", 2.0)]
        }
```

##### Rust (Synchronous)

```rust
        let _: Result<i32, _> = r.zadd_multiple("myzset", &[(1, "one"), (2, "two"), (3, "three")]);

        if let Ok(res9) = r.zrange_withscores("myzset", 0, 1) {
            let res9: Vec<(String, f64)> = res9;
            println!("{res9:?}"); // >>> [("one", 1.0), ("two", 2.0)]
        }
```



This example shows how to query the sorted set by score, excluding the value `1` and up to infinity, returning only the second element of the result:

Query by score: Query a sorted set by score range using ZRANGE with BYSCORE and LIMIT options (supports exclusive ranges and pagination)

**Difficulty:** Intermediate

**Commands:** DEL, ZADD, ZRANGE

**Complexity:**
- DEL: O(N)
- ZADD: O(log(N)
- ZRANGE: O(log(N)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> DEL myzset
(integer) 1
> ZADD myzset 1 "one" 2 "two" 3 "three"
(integer) 3
> ZRANGE myzset (1 +inf BYSCORE LIMIT 1 1
1) "three"
```

##### C#

```csharp
        long zRangeResult7 = db.SortedSetAdd(
            "myzset",
            [
                new("one", 1),
                new("two", 2),
                new("three", 3)
            ]
        );

        RedisValue[] zRangeResult8 = db.SortedSetRangeByScore(
            "myzset",
            1,
            double.PositiveInfinity,
            Exclude.Start,
            skip: 1, take: 1
        );
        Console.WriteLine(string.Join(", ", zRangeResult8));
        // >>> three
```

##### Go

```go
	zRangeResult7, err := rdb.ZAdd(ctx, "myzset",
		redis.Z{Member: "one", Score: 1},
		redis.Z{Member: "two", Score: 2},
		redis.Z{Member: "three", Score: 3},
	).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zRangeResult7) // >>> 3

	zRangeResult8, err := rdb.ZRangeArgs(ctx,
		redis.ZRangeArgs{
			Key:     "myzset",
			ByScore: true,
			Start:   "(1",
			Stop:    "+inf",
			Offset:  1,
			Count:   1,
		},
	).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(zRangeResult8) // >>> [three]
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> zRange3Example = asyncCommands.zadd("myzset",
                    ScoredValue.just(1d, "one"), ScoredValue.just(2d, "two"), ScoredValue.just(3d, "three"))
                    .thenCompose(res11 -> {
                        return asyncCommands.zrangebyscore("myzset",
                                Range.from(Range.Boundary.excluding(1d), Range.Boundary.unbounded()),
                                Limit.create(1, 1));
                    }).thenAccept(res12 -> {
                        System.out.println(res12); // >>> [three]
                    }).toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> zRange3Example = reactiveCommands.zadd("myzset",
                    ScoredValue.just(1d, "one"), ScoredValue.just(2d, "two"), ScoredValue.just(3d, "three"))
                    .then(reactiveCommands.zrangebyscore("myzset",
                            Range.from(Range.Boundary.excluding(1d), Range.Boundary.unbounded()),
                            Limit.create(1, 1)).collectList())
                    .doOnNext(res10 -> {
                        System.out.println(res10); // >>> [three]
                    })
                    .then();
```

##### Java (Synchronous - Jedis)

```java
        Map<String, Double> zRangeExampleParams3 = new HashMap<>();
        zRangeExampleParams3.put("one", 1.0);
        zRangeExampleParams3.put("two", 2.0);
        zRangeExampleParams3.put("three", 3.0);
        long zRangeResult7 = jedis.zadd("myzset", zRangeExampleParams3);
        System.out.println(zRangeResult7);  // >>> 3

        List<String> zRangeResult8 = jedis.zrangeByScore("myzset", "(1", "+inf", 1, 1);
        System.out.println(String.join(", ", zRangeResult8));   // >>> three
```

##### JavaScript (Node.js)

```javascript
const val11 = await client.zAdd("myzset", [
  { value: 'one', score: 1 },
  { value: 'two', score: 2 },
  { value: 'three', score: 3 }
]);
console.log(val11);
// returns 3

const val12 = await client.zRange('myzset', 2, 3, { BY: 'SCORE', LIMIT: { offset: 1, count: 1 } });
console.log(val12);
// >>> ['three']
```

##### JavaScript (Node.js)

```javascript
await redis.zadd('myzset', 1, 'one', 2, 'two', 3, 'three');

const res10 = await redis.zrange('myzset', '(1', '+inf', 'BYSCORE', 'LIMIT', 1, 1);
console.log(res10); // >>> ['three']
```

##### JavaScript (Node.js)

```javascript
const val11 = await client.zAdd("myzset", [
  { value: 'one', score: 1 },
  { value: 'two', score: 2 },
  { value: 'three', score: 3 }
]);
console.log(val11);
// returns 3

const val12 = await client.zRange('myzset', 2, 3, { BY: 'SCORE', LIMIT: { offset: 1, count: 1 } });
console.log(val12);
// >>> ['three']
```

##### JavaScript (Node.js)

```javascript
await redis.zadd('myzset', 1, 'one', 2, 'two', 3, 'three');

const res10 = await redis.zrange('myzset', '(1', '+inf', 'BYSCORE', 'LIMIT', 1, 1);
console.log(res10); // >>> ['three']
```

##### PHP

```php
        $r->zadd('myzset', ['one' => 1, 'two' => 2, 'three' => 3]);

        $res10 = $r->zrange('myzset', '(1', '+inf', ['byscore' => true, 'limit' => [1, 1]]);
        echo json_encode($res10) . PHP_EOL; // >>> ["three"]
```

##### Python

```python
res = r.zadd("myzset", {"one": 1, "two":2, "three":3})
res = r.zrange("myzset", 2, 3, byscore=True, offset=1, num=1)
print(res)
# >>> ['three']
```

##### Ruby

```ruby
r.zadd('myzset', [[1, 'one'], [2, 'two'], [3, 'three']])

res10 = r.zrange('myzset', '(1', '+inf', by_score: true, limit: [1, 1])
puts res10.inspect # >>> ["three"]
```

##### Rust (Asynchronous)

```rust
        let _: Result<i32, _> = r.zadd_multiple("myzset", &[(1, "one"), (2, "two"), (3, "three")]).await;

        if let Ok(res10) = r.zrangebyscore_limit("myzset", "(1", "+inf", 1, 1).await {
            let res10: Vec<String> = res10;
            println!("{res10:?}"); // >>> ["three"]
        }
```

##### Rust (Synchronous)

```rust
        let _: Result<i32, _> = r.zadd_multiple("myzset", &[(1, "one"), (2, "two"), (3, "three")]);

        if let Ok(res10) = r.zrangebyscore_limit("myzset", "(1", "+inf", 1, 1) {
            let res10: Vec<String> = res10;
            println!("{res10:?}"); // >>> ["three"]
        }
```



## Details

### Common behavior and options

The order of elements is from the lowest to the highest score. Elements with the same score are ordered lexicographically.

The optional `REV` argument reverses the ordering, so elements are ordered from highest to lowest score, and score ties are resolved by reverse lexicographical ordering.

The optional `LIMIT` argument can be used to obtain a sub-range from the matching elements (similar to _SELECT LIMIT offset, count_ in SQL).
A negative `<count>` returns all elements from the `<offset>`. Keep in mind that if `<offset>` is large, the sorted set needs to be traversed for `<offset>` elements before getting to the elements to return, which can add up to O(N) time complexity.

The optional `WITHSCORES` argument supplements the command's reply with the scores of elements returned. The returned list contains `value1,score1,...,valueN,scoreN` instead of `value1,...,valueN`. Client libraries are free to return a more appropriate data type (suggestion: an array with (value, score) arrays/tuples).

### Index ranges

By default, the command performs an index range query. The `<start>` and `<stop>` arguments represent zero-based indexes, where `0` is the first element, `1` is the next element, and so on. These arguments specify an **inclusive range**, so for example, `ZRANGE myzset 0 1` will return both the first and the second element of the sorted set.

The indexes can also be negative numbers indicating offsets from the end of the sorted set, with `-1` being the last element of the sorted set, `-2` the penultimate element, and so on.

Out of range indexes do not produce an error.

If `<start>` is greater than either the end index of the sorted set or `<stop>`, an empty list is returned.

If `<stop>` is greater than the end index of the sorted set, Redis will use the last element of the sorted set.

### Score ranges

When the `BYSCORE` option is provided, the command behaves like [`ZRANGEBYSCORE`](https://redis.io/docs/latest/commands/zrangebyscore) and returns the range of elements from the sorted set having scores equal or between `<start>` and `<stop>`.

`<start>` and `<stop>` can be `-inf` and `+inf`, denoting the negative and positive infinities, respectively. This means that you are not required to know the highest or lowest score in the sorted set to get all elements from or up to a certain score.

By default, the score intervals specified by `<start>` and `<stop>` are closed (inclusive).
It is possible to specify an open interval (exclusive) by prefixing the score
with the character `(`.

For example:

```
ZRANGE zset (1 5 BYSCORE
```

Will return all elements with `1 < score <= 5` while:

```
ZRANGE zset (5 (10 BYSCORE
```

Will return all the elements with `5 < score < 10` (5 and 10 excluded).

### Reverse ranges

Using the `REV` option reverses the sorted set, with index 0 as the element with the highest score.

By default, `<start>` must be less than or equal to `<stop>` to return anything.
However, if the `BYSCORE`, or `BYLEX` options are selected, the `<start>` is the highest score to consider, and `<stop>` is the lowest score to consider, therefore `<start>` must be greater than or equal to `<stop>` in order to return anything.

For example:

```
ZRANGE zset 5 10 REV
```

Will return the elements between index 5 and 10 in the reversed index.

```
ZRANGE zset 10 5 REV BYSCORE
```

Will return all elements with scores less than 10 and greater than 5.

### Lexicographical ranges

When the `BYLEX` option is used, the command behaves like [`ZRANGEBYLEX`](https://redis.io/docs/latest/commands/zrangebylex) and returns the range of elements from the sorted set between the `<start>` and `<stop>` lexicographical closed range intervals.

Note that lexicographical ordering relies on all elements having the same score. The reply is unspecified when the elements have different scores.

Valid `<start>` and `<stop>` must start with `(` or `[`, in order to specify
whether the range interval is exclusive or inclusive, respectively.

The special values of `+` or `-` for `<start>` and `<stop>` mean positive and negative infinite strings, respectively, so for instance the command `ZRANGE myzset - + BYLEX` is guaranteed to return all the elements in the sorted set, providing that all the elements have the same score.

The `REV` options reverses the order of the `<start>` and `<stop>` elements, where `<start>` must be lexicographically greater than `<stop>` to produce a non-empty result.

#### Lexicographical comparison of strings

Strings are compared as a binary array of bytes. Because of how the ASCII character set is specified, this means that usually this also have the effect of comparing normal ASCII characters in an obvious dictionary way. However, this is not true if non-plain ASCII strings are used (for example, utf8 strings).

However, the user can apply a transformation to the encoded string so that the first part of the element inserted in the sorted set will compare as the user requires for the specific application. For example, if I want to
add strings that will be compared in a case-insensitive way, but I still
want to retrieve the real case when querying, I can add strings in the
following way:

    ZADD autocomplete 0 foo:Foo 0 bar:BAR 0 zap:zap

Because of the first *normalized* part in every element (before the colon character), we are forcing a given comparison. However, after the range is queried using `ZRANGE ... BYLEX`, the application can display to the user the second part of the string, after the colon.

The binary nature of the comparison allows to use sorted sets as a general purpose index, for example, the first part of the element can be a 64-bit big-endian number. Since big-endian numbers have the most significant bytes in the initial positions, the binary comparison will match the numerical comparison of the numbers. This can be used in order to implement range queries on 64-bit values. As in the example below, after the first 8 bytes, we can store the value of the element we are indexing.

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Array reply](../../develop/reference/protocol-spec#arrays): a list of members in the specified range with, optionally, their scores when the _WITHSCORES_ option is given.

**RESP3:**

[Array reply](../../develop/reference/protocol-spec#arrays): a list of members in the specified range with, optionally, their scores when the _WITHSCORES_ option is given.



