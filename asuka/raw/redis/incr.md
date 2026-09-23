# INCR

```json metadata
{
  "schema_version": 2,
  "title": "INCR",
  "description": "Increments the integer value of a key by one. Uses 0 as initial value if the key doesn't exist.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"}],
  "syntax_fmt": "INCR key",
  "complexity": "O(1)",
  "group": "string",
  "command_flags": ["write","denyoom","fast"],
  "acl_categories": ["@write","@string","@fast"],
  "since": "1.0.0",
  "arity": 2,
  "key_specs": [{"RW":true,"access":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"},"update":true}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"children":[{"id":"pattern-counter","title":"Pattern: counter"},{"id":"pattern-rate-limiter","title":"Pattern: rate limiter"}],"id":"details","title":"Details"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_string-stepincr","commands":[{"acl_categories":["@write","@string","@slow"],"complexity":"O(1)","name":"SET"},{"acl_categories":["@write","@string","@fast"],"complexity":"O(1)","name":"INCR"},{"acl_categories":["@read","@string","@fast"],"complexity":"O(1)","name":"GET"}],"description":"Foundational: Increment the integer value of a key by one using INCR (initializes to 0 if key doesn\u0026amp;#39;t exist)","difficulty":"beginner","id":"incr","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_string-stepincr"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_string-stepincr"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_string-stepincr"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_string-stepincr"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_string-stepincr"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_string-stepincr"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_string-stepincr"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_string-stepincr"},{"clientId":"hiredis","clientName":"hiredis","id":"C","langId":"c","panelId":"panel_C_cmds_string-stepincr"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_string-stepincr"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_string-stepincr"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_string-stepincr"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_string-stepincr"}]}]
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

Increments the number stored at `key` by one.
If the key does not exist, it is set to `0` before performing the operation.
An error is returned if the key contains a value of the wrong type or contains a
string that can not be represented as integer.
This operation is limited to 64 bit signed integers.

Note: this is a string operation because Redis does not have a dedicated
integer type.
The string stored at the key is interpreted as a base-10 64-bit signed
integer to execute the operation.

Redis stores integers in their integer representation, so for string values
that actually hold an integer, there is no overhead for storing the string
representation of the integer.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key.

</details>

## Examples

Foundational: Increment the integer value of a key by one using INCR (initializes to 0 if key doesn't exist)

**Difficulty:** Beginner

**Commands:** SET, INCR, GET

**Complexity:**
- SET: O(1)
- INCR: O(1)
- GET: O(1)

**Available in:** Redis CLI, C, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> SET mykey "10"
OK
> INCR mykey
(integer) 11
> GET mykey
"11"
```

##### C

```c
    reply = redisCommand(c, "SET mykey 10");
    printf("%s\n", reply->str);
    // >>> OK
    freeReplyObject(reply);

    reply = redisCommand(c, "INCR mykey");
    printf("%lld\n", reply->integer);
    // >>> 11
    freeReplyObject(reply);

    reply = redisCommand(c, "GET mykey");
    printf("%s\n", reply->str);
    // >>> 11
    freeReplyObject(reply);
```

##### C#

```csharp
        bool incrResult1 = db.StringSet("mykey", "10");
        Console.WriteLine(incrResult1); // >>> true

        long incrResult2 = db.StringIncrement("mykey");
        Console.WriteLine(incrResult2); // >>> 11

        RedisValue incrResult3 = db.StringGet("mykey");
        Console.WriteLine(incrResult3); // >>> 11
```

##### Go

```go
	incrResult1, err := rdb.Set(ctx, "mykey", "10", 0).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(incrResult1) // >>> OK

	incrResult2, err := rdb.Incr(ctx, "mykey").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(incrResult2) // >>> 11

	incrResult3, err := rdb.Get(ctx, "mykey").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(incrResult3) // >>> 11
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> incrExample = asyncCommands.set("mykey", "10")
                    .thenCompose(incrResult1 -> {
                        System.out.println(incrResult1);    // >>> OK
                        return asyncCommands.incr("mykey");
                    })
                    .thenCompose(incrResult2 -> {
                        System.out.println(incrResult2);    // >>> 11
                        return asyncCommands.get("mykey");
                    })
                    .thenAccept(incrResult3 -> {
                        System.out.println(incrResult3);    // >>> 11
                    })
                    .toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> incrExample = reactiveCommands.set("mykey", "10")
                    .flatMap(incrResult1 -> {
                        System.out.println(incrResult1);    // >>> OK
                        return reactiveCommands.incr("mykey");
                    })
                    .flatMap(incrResult2 -> {
                        System.out.println(incrResult2);    // >>> 11
                        return reactiveCommands.get("mykey");
                    })
                    .doOnNext(incrResult3 -> {
                        System.out.println(incrResult3);    // >>> 11
                    })
                    .then();
```

##### Java (Synchronous - Jedis)

```java
        String incrResult1 = jedis.set("mykey", "10");
        System.out.println(incrResult1);    // >>> OK

        long incrResult2 = jedis.incr("mykey");
        System.out.println(incrResult2);    // >>> 11

        String incrResult3 = jedis.get("mykey");
        System.out.println(incrResult3);    // >>> 11
```

##### JavaScript (Node.js)

```javascript
const incrResult1 = await client.set('mykey', '10');
console.log(incrResult1); // >>> OK

const incrResult2 = await client.incr('mykey');
console.log(incrResult2); // >>> 11

const incrResult3 = await client.get('mykey');
console.log(incrResult3); // >>> 11
```

##### JavaScript (Node.js)

```javascript
const incrResult1 = await redis.set('mykey', '10');
console.log(incrResult1); // >>> OK

const incrResult2 = await redis.incr('mykey');
console.log(incrResult2); // >>> 11

const incrResult3 = await redis.get('mykey');
console.log(incrResult3); // >>> 11
```

##### JavaScript (Node.js)

```javascript
const incrResult1 = await client.set('mykey', '10');
console.log(incrResult1); // >>> OK

const incrResult2 = await client.incr('mykey');
console.log(incrResult2); // >>> 11

const incrResult3 = await client.get('mykey');
console.log(incrResult3); // >>> 11
```

##### JavaScript (Node.js)

```javascript
const incrResult1 = await redis.set('mykey', '10');
console.log(incrResult1); // >>> OK

const incrResult2 = await redis.incr('mykey');
console.log(incrResult2); // >>> 11

const incrResult3 = await redis.get('mykey');
console.log(incrResult3); // >>> 11
```

##### PHP

```php
        $incrResult1 = $r->set('mykey', '10');
        echo $incrResult1 . PHP_EOL;        // >>> OK

        $incrResult2 = $r->incr('mykey');
        echo $incrResult2 . PHP_EOL;        // >>> 11

        $incrResult3 = $r->get('mykey');
        echo $incrResult3 . PHP_EOL;        // >>> 11
```

##### Python

```python
incr_result1 = r.set("mykey", "10")
print(incr_result1)
# >>> True

incr_result2 = r.incr("mykey")
print(incr_result2)
# >>> 11

incr_result3 = r.get("mykey")
print(incr_result3)
# >>> 11
```

##### Rust (Asynchronous)

```rust
        if let Ok(res) = r.set("mykey", "10").await {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.incr("mykey", 1).await {
            Ok(incr_result) => {
                let incr_result: i64 = incr_result;
                println!("{incr_result}");    // >>> 11
            }
            Err(e) => {
                println!("Error incrementing value: {e}");
            }
        }

        match r.get("mykey").await {
            Ok(get_result) => {
                let get_result: String = get_result;
                println!("{get_result}");    // >>> 11
            }
            Err(e) => {
                println!("Error getting value: {e}");
            }
        }
```

##### Rust (Synchronous)

```rust
        if let Ok(res) = r.set("mykey", "10") {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.incr("mykey", 1) {
            Ok(incr_result) => {
                let incr_result: i64 = incr_result;
                println!("{incr_result}");    // >>> 11
            }
            Err(e) => {
                println!("Error incrementing value: {e}");
            }
        }

        match r.get("mykey") {
            Ok(get_result) => {
                let get_result: String = get_result;
                println!("{get_result}");    // >>> 11
            }
            Err(e) => {
                println!("Error getting value: {e}");
            }
        }
```



## Details

### Pattern: counter

The counter pattern is the most obvious thing you can do with Redis atomic
increment operations.
The idea is simply send an `INCR` command to Redis every time an operation
occurs.
For instance in a web application we may want to know how many page views this
user did every day of the year.

To do so the web application may simply increment a key every time the user
performs a page view, creating the key name concatenating the User ID and a
string representing the current date.

This simple pattern can be extended in many ways:

* It is possible to use `INCR` and [`EXPIRE`](https://redis.io/docs/latest/commands/expire) together at every page view to have
  a counter counting only the latest N page views separated by less than the
  specified amount of seconds.
* A client may use GETSET in order to atomically get the current counter value
  and reset it to zero.
* Using other atomic increment/decrement commands like [`DECR`](https://redis.io/docs/latest/commands/decr) or [`INCRBY`](https://redis.io/docs/latest/commands/incrby) it
  is possible to handle values that may get bigger or smaller depending on the
  operations performed by the user.
  Imagine for instance the score of different users in an online game.

### Pattern: rate limiter

The rate limiter pattern is a special counter that is used to limit the rate at
which an operation can be performed.
The classical materialization of this pattern involves limiting the number of
requests that can be performed against a public API.

You can implement this pattern with INCR in two ways. Both examples limit API calls to a maximum of ten requests per second per IP address.

#### Pattern: rate limiter 1

The more simple and direct implementation of this pattern is the following:

```
FUNCTION LIMIT_API_CALL(ip)
ts = CURRENT_UNIX_TIME()
keyname = ip+":"+ts
MULTI
    INCR(keyname)
    EXPIRE(keyname,10)
EXEC
current = RESPONSE_OF_INCR_WITHIN_MULTI
IF current > 10 THEN
    ERROR "too many requests per second"
ELSE
    PERFORM_API_CALL()
END
```

In this example, there is a counter for every IP, for every different second.
But these counters are always incremented setting an expire of 10 seconds so that
they'll be removed by Redis automatically when the current second is a different
one.

Note the used of [`MULTI`](https://redis.io/docs/latest/commands/multi) and [`EXEC`](https://redis.io/docs/latest/commands/exec) in order to make sure that we'll both
increment and set the expire at every API call.

#### Pattern: rate limiter 2

An alternative implementation uses a single counter, but is a bit more complex
to get it right without race conditions.
We'll examine different variants.

```
FUNCTION LIMIT_API_CALL(ip):
current = GET(ip)
IF current != NULL AND current > 10 THEN
    ERROR "too many requests per second"
ELSE
    value = INCR(ip)
    IF value == 1 THEN
        EXPIRE(ip,1)
    END
    PERFORM_API_CALL()
END
```

The counter is created in a way that it only will survive one second, starting
from the first request performed in the current second.
If there are more than 10 requests in the same second the counter will reach a
value greater than 10, otherwise it will expire and start again from 0.

**In the above code there is a race condition**.
If for some reason the client performs the `INCR` command but does not perform
the [`EXPIRE`](https://redis.io/docs/latest/commands/expire) the key will be leaked until we'll see the same IP address again.

This can be easily fixed by turning the `INCR` with optional [`EXPIRE`](https://redis.io/docs/latest/commands/expire) into a Lua
script that is then sent using the [`EVAL`](https://redis.io/docs/latest/commands/eval) command (only available since Redis version
2.6).

```
local current
current = redis.call("incr",KEYS[1])
if current == 1 then
    redis.call("expire",KEYS[1],1)
end
```

There is a different way to fix this issue without using scripting, by using
Redis lists instead of counters.
The implementation is more complex and uses more advanced features but has the
advantage of remembering the IP addresses of the clients currently performing an
API call, that may be useful or not depending on the application.

```
FUNCTION LIMIT_API_CALL(ip)
current = LLEN(ip)
IF current > 10 THEN
    ERROR "too many requests per second"
ELSE
    IF EXISTS(ip) == FALSE
        MULTI
            RPUSH(ip,ip)
            EXPIRE(ip,1)
        EXEC
    ELSE
        RPUSHX(ip,ip)
    END
    PERFORM_API_CALL()
END
```

The [`RPUSHX`](https://redis.io/docs/latest/commands/rpushx) command only pushes the element if the key already exists.

Note that we have a race here, but it is not a problem: [`EXISTS`](https://redis.io/docs/latest/commands/exists) may return
false but the key may be created by another client before we create it inside
the [`MULTI`](https://redis.io/docs/latest/commands/multi) / [`EXEC`](https://redis.io/docs/latest/commands/exec) block.
However this race will just miss an API call under rare conditions, so the rate
limiting will still work correctly.

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

[Integer reply](../../develop/reference/protocol-spec#integers): the value of the key after the increment.

**RESP3:**

[Integer reply](../../develop/reference/protocol-spec#integers): the value of the key after the increment.



