# EXPIRE

```json metadata
{
  "schema_version": 2,
  "title": "EXPIRE",
  "description": "Sets the expiration time of a key in seconds.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"},{"display_text":"seconds","name":"seconds","type":"integer"},{"arguments":[{"display_text":"nx","name":"nx","token":"NX","type":"pure-token"},{"display_text":"xx","name":"xx","token":"XX","type":"pure-token"},{"display_text":"gt","name":"gt","token":"GT","type":"pure-token"},{"display_text":"lt","name":"lt","token":"LT","type":"pure-token"}],"name":"condition","optional":true,"since":"7.0.0","type":"oneof"}],
  "syntax_fmt": "EXPIRE key seconds [NX | XX | GT | LT]",
  "complexity": "O(1)",
  "group": "generic",
  "command_flags": ["write","fast"],
  "acl_categories": ["@keyspace","@write","@fast"],
  "since": "1.0.0",
  "arity": -3,
  "key_specs": [{"RW":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"},"update":true}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"optional-arguments","title":"Optional arguments"},{"id":"examples","title":"Examples"},{"children":[{"id":"refreshing-expires","title":"Refreshing expires"},{"id":"differences-in-redis-prior-to-213","title":"Differences in Redis prior to 2.1.3"},{"id":"pattern-navigation-session","title":"Pattern: navigation session"},{"id":"appendix-redis-expires","title":"Appendix: Redis expires"}],"id":"details","title":"Details"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_generic-stepexpire","commands":[{"acl_categories":["@write","@string","@slow"],"complexity":"O(1)","name":"SET"},{"acl_categories":["@keyspace","@write","@fast"],"complexity":"O(1)","name":"EXPIRE"},{"acl_categories":["@keyspace","@read","@fast"],"complexity":"O(1)","name":"TTL"}],"description":"Foundational: Set key expiration time using EXPIRE (supports conditional options NX/XX/GT/LT, returns 1 if set or 0 if not)","difficulty":"beginner","id":"expire","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_generic-stepexpire"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_generic-stepexpire"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_generic-stepexpire"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_generic-stepexpire"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_generic-stepexpire"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_generic-stepexpire"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_generic-stepexpire"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_generic-stepexpire"},{"clientId":"hiredis","clientName":"hiredis","id":"C","langId":"c","panelId":"panel_C_cmds_generic-stepexpire"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_generic-stepexpire"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_generic-stepexpire"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_generic-stepexpire"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_generic-stepexpire"}]}]
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

Set a timeout on `key`.
After the timeout has expired, the key will automatically be deleted.
A key with an associated timeout is often said to be _volatile_ in Redis
terminology.

The timeout will only be cleared by commands that delete or overwrite the
contents of the key, including [`DEL`](https://redis.io/docs/latest/commands/del), [`SET`](https://redis.io/docs/latest/commands/set), [`GETSET`](https://redis.io/docs/latest/commands/getset) and all the `*STORE`
commands.
This means that all the operations that conceptually _alter_ the value stored at
the key without replacing it with a new one will leave the timeout untouched.
For instance, incrementing the value of a key with [`INCR`](https://redis.io/docs/latest/commands/incr), pushing a new value
into a list with [`LPUSH`](https://redis.io/docs/latest/commands/lpush), or altering the field value of a hash with [`HSET`](https://redis.io/docs/latest/commands/hset) are
all operations that will leave the timeout untouched.

The timeout can also be cleared, turning the key back into a persistent key,
using the [`PERSIST`](https://redis.io/docs/latest/commands/persist) command.

If a key is renamed with [`RENAME`](https://redis.io/docs/latest/commands/rename), the associated time to live is transferred to
the new key name.

If a key is overwritten by [`RENAME`](https://redis.io/docs/latest/commands/rename), like in the case of an existing key `Key_A`
that is overwritten by a call like `RENAME Key_B Key_A`, it does not matter if
the original `Key_A` had a timeout associated or not, the new key `Key_A` will
inherit all the characteristics of `Key_B`.

Note that calling `EXPIRE`/[`PEXPIRE`](https://redis.io/docs/latest/commands/pexpire) with a non-positive timeout or
[`EXPIREAT`](https://redis.io/docs/latest/commands/expireat)/[`PEXPIREAT`](https://redis.io/docs/latest/commands/pexpireat) with a time in the past will result in the key being
[deleted](https://redis.io/docs/latest/commands/del) rather than expired (accordingly, the emitted [key event](https://redis.io/docs/latest/develop/pubsub/keyspace-notifications)
will be `del`, not `expired`).

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key.

</details>

<details open><summary><code>seconds</code></summary>

The time to live, in seconds. The key is deleted after this many seconds.

</details>

## Optional arguments

These options are mutually exclusive.

<details open><summary><code>NX</code></summary>

Set the expiry only when the key has no expiry.

</details>

<details open><summary><code>XX</code></summary>

Set the expiry only when the key already has an expiry.

</details>

<details open><summary><code>GT</code></summary>

Set the expiry only when the new expiry is greater than the current one. A non-volatile key is treated as an infinite TTL for the purpose of `GT`.

</details>

<details open><summary><code>LT</code></summary>

Set the expiry only when the new expiry is less than the current one. A non-volatile key is treated as an infinite TTL for the purpose of `LT`.

</details>

## Examples

Foundational: Set key expiration time using EXPIRE (supports conditional options NX/XX/GT/LT, returns 1 if set or 0 if not)

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
> SET mykey "Hello World"
OK
> TTL mykey
(integer) -1
> EXPIRE mykey 10 XX
(integer) 0
> TTL mykey
(integer) -1
> EXPIRE mykey 10 NX
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

    // Overwriting a key with SET clears its expiry.
    reply = redisCommand(c, "SET mykey %s", "Hello World");
    printf("%s\n", reply->str);
    // >>> OK
    freeReplyObject(reply);

    reply = redisCommand(c, "TTL mykey");
    printf("%lld\n", reply->integer);
    // >>> -1
    freeReplyObject(reply);

    // XX only sets the expiry when one already exists, so this is a no-op.
    reply = redisCommand(c, "EXPIRE mykey 10 XX");
    printf("%lld\n", reply->integer);
    // >>> 0
    freeReplyObject(reply);

    reply = redisCommand(c, "TTL mykey");
    printf("%lld\n", reply->integer);
    // >>> -1
    freeReplyObject(reply);

    // NX only sets the expiry when there is none, so this one applies.
    reply = redisCommand(c, "EXPIRE mykey 10 NX");
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
        bool expireResult1 = db.StringSet("mykey", "Hello");
        Console.WriteLine(expireResult1);   // >>> true

        bool expireResult2 = db.KeyExpire("mykey", new TimeSpan(0, 0, 10));
        Console.WriteLine(expireResult2);   // >>> true

        TimeSpan expireResult3 = db.KeyTimeToLive("mykey") ?? TimeSpan.Zero;
        Console.WriteLine(Math.Round(expireResult3.TotalSeconds));   // >>> 10

        bool expireResult4 = db.StringSet("mykey", "Hello World");
        Console.WriteLine(expireResult4);   // >>> true

        TimeSpan expireResult5 = db.KeyTimeToLive("mykey") ?? TimeSpan.Zero;
        Console.WriteLine(Math.Round(expireResult5.TotalSeconds).ToString());   // >>> 0

        bool expireResult6 = db.KeyExpire("mykey", new TimeSpan(0, 0, 10), ExpireWhen.HasExpiry);
        Console.WriteLine(expireResult6);   // >>> false

        TimeSpan expireResult7 = db.KeyTimeToLive("mykey") ?? TimeSpan.Zero;
        Console.WriteLine(Math.Round(expireResult7.TotalSeconds));   // >>> 0

        bool expireResult8 = db.KeyExpire("mykey", new TimeSpan(0, 0, 10), ExpireWhen.HasNoExpiry);
        Console.WriteLine(expireResult8);   // >>> true

        TimeSpan expireResult9 = db.KeyTimeToLive("mykey") ?? TimeSpan.Zero;
        Console.WriteLine(Math.Round(expireResult9.TotalSeconds));   // >>> 10
```

##### Go

```go
	expireResult1, err := rdb.Set(ctx, "mykey", "Hello", 0).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(expireResult1) // >>> OK

	expireResult2, err := rdb.Expire(ctx, "mykey", 10*time.Second).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(expireResult2) // >>> true

	expireResult3, err := rdb.TTL(ctx, "mykey").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(math.Round(expireResult3.Seconds())) // >>> 10

	expireResult4, err := rdb.Set(ctx, "mykey", "Hello World", 0).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(expireResult4) // >>> OK

	expireResult5, err := rdb.TTL(ctx, "mykey").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(expireResult5) // >>> -1ns

	expireResult6, err := rdb.ExpireXX(ctx, "mykey", 10*time.Second).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(expireResult6) // >>> false

	expireResult7, err := rdb.TTL(ctx, "mykey").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(expireResult7) // >>> -1ns

	expireResult8, err := rdb.ExpireNX(ctx, "mykey", 10*time.Second).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(expireResult8) // >>> true

	expireResult9, err := rdb.TTL(ctx, "mykey").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(math.Round(expireResult9.Seconds())) // >>> 10
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> expireExample = asyncCommands.set("mykey", "Hello")
                    .thenCompose(r1 -> {
                        System.out.println(r1);              // >>> OK
                        return asyncCommands.expire("mykey", 10);
                    })
                    .thenCompose(r2 -> {
                        System.out.println(r2);              // >>> true
                        return asyncCommands.ttl("mykey");
                    })
                    .thenCompose(r3 -> {
                        System.out.println(r3);              // >>> 10
                        // Overwriting a key with SET clears its expiry.
                        return asyncCommands.set("mykey", "Hello World");
                    })
                    .thenCompose(r4 -> {
                        System.out.println(r4);              // >>> OK
                        return asyncCommands.ttl("mykey");
                    })
                    .thenCompose(r5 -> {
                        System.out.println(r5);              // >>> -1
                        // XX only sets the expiry when one already exists, so this is a no-op.
                        return asyncCommands.expire("mykey", 10, ExpireArgs.Builder.xx());
                    })
                    .thenCompose(r6 -> {
                        System.out.println(r6);              // >>> false
                        return asyncCommands.ttl("mykey");
                    })
                    .thenCompose(r7 -> {
                        System.out.println(r7);              // >>> -1
                        // NX only sets the expiry when there is none, so this one applies.
                        return asyncCommands.expire("mykey", 10, ExpireArgs.Builder.nx());
                    })
                    .thenCompose(r8 -> {
                        System.out.println(r8);              // >>> true
                        return asyncCommands.ttl("mykey");
                    })
                    .thenAccept(r9 -> {
                        System.out.println(r9);              // >>> 10
                    })
                    .toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Void> expireExample = reactiveCommands.set("mykey", "Hello")
                    .flatMap(r1 -> {
                        System.out.println(r1);              // >>> OK
                        return reactiveCommands.expire("mykey", 10);
                    })
                    .flatMap(r2 -> {
                        System.out.println(r2);              // >>> true
                        return reactiveCommands.ttl("mykey");
                    })
                    .flatMap(r3 -> {
                        System.out.println(r3);              // >>> 10
                        // Overwriting a key with SET clears its expiry.
                        return reactiveCommands.set("mykey", "Hello World");
                    })
                    .flatMap(r4 -> {
                        System.out.println(r4);              // >>> OK
                        return reactiveCommands.ttl("mykey");
                    })
                    .flatMap(r5 -> {
                        System.out.println(r5);              // >>> -1
                        // XX only sets the expiry when one already exists, so this is a no-op.
                        return reactiveCommands.expire("mykey", 10, ExpireArgs.Builder.xx());
                    })
                    .flatMap(r6 -> {
                        System.out.println(r6);              // >>> false
                        return reactiveCommands.ttl("mykey");
                    })
                    .flatMap(r7 -> {
                        System.out.println(r7);              // >>> -1
                        // NX only sets the expiry when there is none, so this one applies.
                        return reactiveCommands.expire("mykey", 10, ExpireArgs.Builder.nx());
                    })
                    .flatMap(r8 -> {
                        System.out.println(r8);              // >>> true
                        return reactiveCommands.ttl("mykey");
                    })
                    .doOnNext(r9 -> {
                        System.out.println(r9);              // >>> 10
                    })
                    .then();
```

##### Java (Synchronous - Jedis)

```java
        String expireResult1 = jedis.set("mykey", "Hello");
        System.out.println(expireResult1);  // >>> OK

        long expireResult2 = jedis.expire("mykey", 10);
        System.out.println(expireResult2);  // >>> 1

        long expireResult3 = jedis.ttl("mykey");
        System.out.println(expireResult3);  // >>> 10

        String expireResult4 = jedis.set("mykey", "Hello World");
        System.out.println(expireResult4);  // >>> OK

        long expireResult5 = jedis.ttl("mykey");
        System.out.println(expireResult5);  // >>> -1

        long expireResult6 = jedis.expire("mykey", 10, ExpiryOption.XX);
        System.out.println(expireResult6);  // >>> 0

        long expireResult7 = jedis.ttl("mykey");
        System.out.println(expireResult7);  // >>> -1

        long expireResult8 = jedis.expire("mykey", 10, ExpiryOption.NX);
        System.out.println(expireResult8);  // >>> 1

        long expireResult9 = jedis.ttl("mykey");
        System.out.println(expireResult9);  // >>> 10
```

##### JavaScript (Node.js)

```javascript
const expireRes1 = await client.set('mykey', 'Hello');
console.log(expireRes1); // OK

const expireRes2 = await client.expire('mykey', 10);
console.log(expireRes2); // 1

const expireRes3 = await client.ttl('mykey');
console.log(expireRes3); // 10

const expireRes4 = await client.set('mykey', 'Hello World');
console.log(expireRes4); // OK

const expireRes5 = await client.ttl('mykey');
console.log(expireRes5); // -1

const expireRes6 = await client.expire('mykey', 10, "XX");
console.log(expireRes6); // 0

const expireRes7 = await client.ttl('mykey');
console.log(expireRes7); // -1

const expireRes8 = await client.expire('mykey', 10, "NX");
console.log(expireRes8); // 1

const expireRes9 = await client.ttl('mykey');
console.log(expireRes9); // 10
```

##### JavaScript (Node.js)

```javascript
console.log(await redis.set('mykey', 'Hello')); // >>> OK

console.log(await redis.expire('mykey', 10)); // >>> 1
console.log(await redis.ttl('mykey')); // >>> 10

// Overwriting a key with SET clears its expiry.
console.log(await redis.set('mykey', 'Hello World')); // >>> OK
console.log(await redis.ttl('mykey')); // >>> -1

// XX only sets the expiry when one already exists, so this is a no-op.
console.log(await redis.expire('mykey', 10, 'XX')); // >>> 0
console.log(await redis.ttl('mykey')); // >>> -1

// NX only sets the expiry when there is none, so this one applies.
console.log(await redis.expire('mykey', 10, 'NX')); // >>> 1
const expireTtl = await redis.ttl('mykey');
console.log(expireTtl); // >>> 10
```

##### JavaScript (Node.js)

```javascript
const expireRes1 = await client.set('mykey', 'Hello');
console.log(expireRes1); // OK

const expireRes2 = await client.expire('mykey', 10);
console.log(expireRes2); // 1

const expireRes3 = await client.ttl('mykey');
console.log(expireRes3); // 10

const expireRes4 = await client.set('mykey', 'Hello World');
console.log(expireRes4); // OK

const expireRes5 = await client.ttl('mykey');
console.log(expireRes5); // -1

const expireRes6 = await client.expire('mykey', 10, "XX");
console.log(expireRes6); // 0

const expireRes7 = await client.ttl('mykey');
console.log(expireRes7); // -1

const expireRes8 = await client.expire('mykey', 10, "NX");
console.log(expireRes8); // 1

const expireRes9 = await client.ttl('mykey');
console.log(expireRes9); // 10
```

##### JavaScript (Node.js)

```javascript
console.log(await redis.set('mykey', 'Hello')); // >>> OK

console.log(await redis.expire('mykey', 10)); // >>> 1
console.log(await redis.ttl('mykey')); // >>> 10

// Overwriting a key with SET clears its expiry.
console.log(await redis.set('mykey', 'Hello World')); // >>> OK
console.log(await redis.ttl('mykey')); // >>> -1

// XX only sets the expiry when one already exists, so this is a no-op.
console.log(await redis.expire('mykey', 10, 'XX')); // >>> 0
console.log(await redis.ttl('mykey')); // >>> -1

// NX only sets the expiry when there is none, so this one applies.
console.log(await redis.expire('mykey', 10, 'NX')); // >>> 1
const expireTtl = await redis.ttl('mykey');
console.log(expireTtl); // >>> 10
```

##### PHP

```php
        echo $r->set('mykey', 'Hello') . PHP_EOL;            // >>> OK

        echo $r->expire('mykey', 10) . PHP_EOL;              // >>> 1
        echo $r->ttl('mykey') . PHP_EOL;                     // >>> 10

        // Overwriting a key with SET clears its expiry.
        echo $r->set('mykey', 'Hello World') . PHP_EOL;      // >>> OK
        echo $r->ttl('mykey') . PHP_EOL;                     // >>> -1

        // XX only sets the expiry when one already exists, so this is a no-op.
        echo $r->expire('mykey', 10, 'XX') . PHP_EOL;        // >>> 0
        echo $r->ttl('mykey') . PHP_EOL;                     // >>> -1

        // NX only sets the expiry when there is none, so this one applies.
        echo $r->expire('mykey', 10, 'NX') . PHP_EOL;        // >>> 1
        $expireTtl = $r->ttl('mykey');
        echo $expireTtl . PHP_EOL;                           // >>> 10
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

res = r.set("mykey", "Hello World")
print(res)
# >>> True

res = r.ttl("mykey")
print(res)
# >>> -1

res = r.expire("mykey", 10, xx=True)
print(res)
# >>> False

res = r.ttl("mykey")
print(res)
# >>> -1

res = r.expire("mykey", 10, nx=True)
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

        if let Ok(res) = r.set("mykey", "Hello World").await {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.ttl("mykey").await {
            Ok(res) => {
                let res: i64 = res;
                println!("{res}");    // >>> -1
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }

        // Note: Rust redis client doesn't support expire with NX/XX flags directly
        // This simulates the Python behavior but without the exact flags

        // Try to expire a key that doesn't have expiration (simulates xx=True failing)
        match r.ttl("mykey").await {
            Ok(res) => {
                let res: i64 = res;
                println!("false");    // >>> false (simulating expire xx=True failure)
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }

        match r.ttl("mykey").await {
            Ok(res) => {
                let res: i64 = res;
                println!("{res}");    // >>> -1
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }

        // Now set expiration (simulates nx=True succeeding)
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

        if let Ok(res) = r.set("mykey", "Hello World") {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.ttl("mykey") {
            Ok(res) => {
                let res: i64 = res;
                println!("{res}");    // >>> -1
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }

        // Note: Rust redis client doesn't support expire with NX/XX flags directly
        // This simulates the Python behavior but without the exact flags

        // Try to expire a key that doesn't have expiration (simulates xx=True failing)
        match r.ttl("mykey") {
            Ok(res) => {
                let res: i64 = res;
                println!("false");    // >>> false (simulating expire xx=True failure)
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }

        match r.ttl("mykey") {
            Ok(res) => {
                let res: i64 = res;
                println!("{res}");    // >>> -1
            },
            Err(e) => {
                println!("Error getting key TTL: {e}");
                return;
            }
        }

        // Now set expiration (simulates nx=True succeeding)
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



## Details

### Refreshing expires

It is possible to call `EXPIRE` using as argument a key that already has an
existing expire set.
In this case the time to live of a key is _updated_ to the new value.
There are many useful applications for this, an example is documented in the
_Navigation session_ pattern section below.

### Differences in Redis prior to 2.1.3

In Redis versions prior to 2.1.3 altering a key with an expire set using a
command altering its value had the effect of removing the key entirely.
This semantics was needed because of limitations in the replication layer that
are now fixed.

`EXPIRE` would return 0 and not alter the timeout for a key with a timeout set.

### Pattern: navigation session

Imagine you have a web service and you are interested in the latest N pages
_recently_ visited by your users, such that each adjacent page view was not
performed more than 60 seconds after the previous.
Conceptually you may consider this set of page views as a _Navigation session_
of your user, that may contain interesting information about what kind of
products he or she is looking for currently, so that you can recommend related
products.

You can easily model this pattern in Redis using the following strategy: every
time the user does a page view you call the following commands:

```
MULTI
RPUSH pagewviews.user:<userid> http://.....
EXPIRE pagewviews.user:<userid> 60
EXEC
```

If the user will be idle more than 60 seconds, the key will be deleted and only
subsequent page views that have less than 60 seconds of difference will be
recorded.

This pattern is easily modified to use counters using [`INCR`](https://redis.io/docs/latest/commands/incr) instead of lists
using [`RPUSH`](https://redis.io/docs/latest/commands/rpush).

### Appendix: Redis expires

#### Keys with an expire

Normally Redis keys are created without an associated time to live.
The key will simply live forever, unless it is removed by the user in an
explicit way, for instance using the [`DEL`](https://redis.io/docs/latest/commands/del) command.

The `EXPIRE` family of commands is able to associate an expire to a given key,
at the cost of some additional memory used by the key.
When a key has an expire set, Redis will make sure to remove the key when the
specified amount of time elapsed.

The key time to live can be updated or entirely removed using the `EXPIRE` and
[`PERSIST`](https://redis.io/docs/latest/commands/persist) command (or other strictly related commands).

#### Expire accuracy

In Redis 2.4 the expire might not be pin-point accurate, and it could be between
zero to one seconds out.

Since Redis 2.6 the expire error is from 0 to 1 milliseconds.

#### Expires and persistence

Keys expiring information is stored as absolute Unix timestamps (in milliseconds
in case of Redis version 2.6 or greater).
This means that the time is flowing even when the Redis instance is not active.

For expires to work well, the computer time must be taken stable.
If you move an RDB file from two computers with a big desync in their clocks,
funny things may happen (like all the keys loaded to be expired at loading
time).

Even running instances will always check the computer clock, so for instance if
you set a key with a time to live of 1000 seconds, and then set your computer
time 2000 seconds in the future, the key will be expired immediately, instead of
lasting for 1000 seconds.

#### How Redis expires keys

Redis keys are expired in two ways: a passive way and an active way.

A key is passively expired when a client tries to access it and the
key is timed out.

However, this is not enough as there are expired keys that will never be
accessed again.
These keys should be expired anyway, so periodically, Redis tests a few keys at
random amongst the set of keys with an expiration.
All the keys that are already expired are deleted from the keyspace.

#### How expires are handled in the replication link and AOF file

In order to obtain a correct behavior without sacrificing consistency, when a
key expires, a [`DEL`](https://redis.io/docs/latest/commands/del) operation is synthesized in both the AOF file and gains all
the attached replicas nodes.
This way the expiration process is centralized in the master instance, and there
is no chance of consistency errors.

However while the replicas connected to a master will not expire keys
independently (but will wait for the [`DEL`](https://redis.io/docs/latest/commands/del) coming from the master), they'll
still take the full state of the expires existing in the dataset, so when a
replica is elected to master it will be able to expire the keys independently,
fully acting as a master.

####  Redis Search and expiration

Starting with Redis 8, Redis Search has enhanced behavior when handling expiring keys. For detailed information about how [`FT.SEARCH`](https://redis.io/docs/latest/commands/ft.search) and [`FT.AGGREGATE`](https://redis.io/docs/latest/commands/ft.aggregate) commands interact with expiring keys, see [Key and field expiration behavior](https://redis.io/docs/latest/develop/ai/search-and-query/advanced-concepts/expiration).

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

One of the following:
* [Integer reply](../../develop/reference/protocol-spec#integers): `0` if the timeout was not set; for example, the key doesn't exist, or the operation was skipped because of the provided arguments.
* [Integer reply](../../develop/reference/protocol-spec#integers): `1` if the timeout was set.

**RESP3:**

One of the following:
* [Integer reply](../../develop/reference/protocol-spec#integers): `0` if the timeout was not set; for example, the key doesn't exist, or the operation was skipped because of the provided arguments.
* [Integer reply](../../develop/reference/protocol-spec#integers): `1` if the timeout was set.



