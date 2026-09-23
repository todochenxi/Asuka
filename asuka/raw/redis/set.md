# SET

```json metadata
{
  "schema_version": 2,
  "title": "SET",
  "description": "Sets the string value of a key, ignoring its type. The key is created if it doesn't exist.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"},{"display_text":"value","name":"value","type":"string"},{"arguments":[{"display_text":"nx","name":"nx","token":"NX","type":"pure-token"},{"display_text":"xx","name":"xx","token":"XX","type":"pure-token"},{"display_text":"ifeq-value","name":"ifeq-value","since":"8.4.0","token":"IFEQ","type":"string"},{"display_text":"ifne-value","name":"ifne-value","since":"8.4.0","token":"IFNE","type":"string"},{"display_text":"ifdeq-digest","name":"ifdeq-digest","since":"8.4.0","token":"IFDEQ","type":"string"},{"display_text":"ifdne-digest","name":"ifdne-digest","since":"8.4.0","token":"IFDNE","type":"string"}],"name":"condition","optional":true,"since":"2.6.12","type":"oneof"},{"display_text":"get","name":"get","optional":true,"since":"6.2.0","token":"GET","type":"pure-token"},{"arguments":[{"display_text":"seconds","name":"seconds","since":"2.6.12","token":"EX","type":"integer"},{"display_text":"milliseconds","name":"milliseconds","since":"2.6.12","token":"PX","type":"integer"},{"display_text":"unix-time-seconds","name":"unix-time-seconds","since":"6.2.0","token":"EXAT","type":"unix-time"},{"display_text":"unix-time-milliseconds","name":"unix-time-milliseconds","since":"6.2.0","token":"PXAT","type":"unix-time"},{"display_text":"keepttl","name":"keepttl","since":"6.0.0","token":"KEEPTTL","type":"pure-token"}],"name":"expiration","optional":true,"type":"oneof"}],
  "syntax_fmt": "SET key value [NX | XX | IFEQ ifeq-value | IFNE ifne-value |\n  IFDEQ ifdeq-digest | IFDNE ifdne-digest] [GET] [EX seconds |\n  PX milliseconds | EXAT unix-time-seconds |\n  PXAT unix-time-milliseconds | KEEPTTL]",
  "complexity": "O(1)",
  "group": "string",
  "command_flags": ["write","denyoom"],
  "acl_categories": ["@write","@string","@slow"],
  "since": "1.0.0",
  "arity": -3,
  "key_specs": [{"RW":true,"access":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"},"notes":"RW and ACCESS due to the optional `GET` argument","update":true,"variable_flags":true}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"optional-arguments","title":"Optional arguments"},{"id":"examples","title":"Examples"},{"children":[{"id":"hash-digest","title":"Hash digest"},{"id":"patterns","title":"Patterns"}],"id":"details","title":"Details"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"set_and_get-stepset","commands":[{"acl_categories":["@write","@string","@slow"],"complexity":"O(1)","name":"SET"},{"acl_categories":["@read","@string","@fast"],"complexity":"O(1)","name":"GET"}],"description":"Foundational: Set the string value of a key using SET (creates key if needed, overwrites existing value, supports expiration options)","difficulty":"beginner","id":"set","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_set_and_get-stepset"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_set_and_get-stepset"},{"id":"Node-js","panelId":"panel_Nodejs_set_and_get-stepset"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_set_and_get-stepset"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_set_and_get-stepset"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_set_and_get-stepset"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_set_and_get-stepset"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_set_and_get-stepset"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_set_and_get-stepset"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_set_and_get-stepset"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_set_and_get-stepset"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_set_and_get-stepset"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_set_and_get-stepset"}]}]
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

Set `key` to hold the string `value`.
If `key` already holds a value, it is overwritten, regardless of its type.
Any previous time to live associated with the key is discarded on successful `SET` operation.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key.

</details>

<details open><summary><code>value</code></summary>

The string value to set.

</details>

## Optional arguments

The following options modify the command's behavior. The condition options (`NX`, `XX`, `IFEQ`, `IFNE`, `IFDEQ`, `IFDNE`) are mutually exclusive, as are the expiration options (`EX`, `PX`, `EXAT`, `PXAT`, `KEEPTTL`).

<details open><summary><code>NX</code></summary>

Only set the key if it does not already exist.

</details>

<details open><summary><code>XX</code></summary>

Only set the key if it already exists.

</details>

<details open><summary><code>IFEQ ifeq-value</code></summary>

Set the key's value and expiration only if its current value is equal to `ifeq-value`. If the key doesn't exist, it won't be created.

</details>

<details open><summary><code>IFNE ifne-value</code></summary>

Set the key's value and expiration only if its current value is not equal to `ifne-value`. If the key doesn't exist, it will be created.

</details>

<details open><summary><code>IFDEQ ifdeq-digest</code></summary>

Set the key's value and expiration only if the hash digest of its current value is equal to `ifdeq-digest`. If the key doesn't exist, it won't be created. See the [Hash Digest](#hash-digest) section below for more information.

</details>

<details open><summary><code>IFDNE ifdne-digest</code></summary>

Set the key's value and expiration only if the hash digest of its current value is not equal to `ifdne-digest`. If the key doesn't exist, it will be created. See the [Hash Digest](#hash-digest) section below for more information.

</details>

<details open><summary><code>GET</code></summary>

Return the old string stored at the key, or nil if the key did not exist. An error is returned and `SET` is aborted if the value stored at the key is not a string.

</details>

<details open><summary><code>EX seconds</code></summary>

Set the specified expire time, in seconds (a positive integer).

</details>

<details open><summary><code>PX milliseconds</code></summary>

Set the specified expire time, in milliseconds (a positive integer).

</details>

<details open><summary><code>EXAT unix-time-seconds</code></summary>

Set the specified Unix time at which the key will expire, in seconds (a positive integer).

</details>

<details open><summary><code>PXAT unix-time-milliseconds</code></summary>

Set the specified Unix time at which the key will expire, in milliseconds (a positive integer).

</details>

<details open><summary><code>KEEPTTL</code></summary>

Retain the time to live associated with the key.

</details>

Note: Since the `SET` command options can replace [`SETNX`](https://redis.io/docs/latest/commands/setnx), [`SETEX`](https://redis.io/docs/latest/commands/setex), [`PSETEX`](https://redis.io/docs/latest/commands/psetex), [`GETSET`](https://redis.io/docs/latest/commands/getset), it is possible that in future versions of Redis these commands will be deprecated and finally removed.

## Examples

Foundational: Set the string value of a key using SET (creates key if needed, overwrites existing value, supports expiration options)

**Difficulty:** Beginner

**Commands:** SET, GET

**Complexity:**
- SET: O(1)
- GET: O(1)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> SET mykey "Hello"
OK
> GET mykey
"Hello"
> SET anotherkey "will expire in a minute" EX 60
OK
```

##### C#

```csharp

using NRedisStack.Tests;
using StackExchange.Redis;

public class SetGetExample
{
    public void Run()
    {
        var muxer = ConnectionMultiplexer.Connect("localhost:6379");
        var db = muxer.GetDatabase();

        bool status = db.StringSet("bike:1", "Process 134");

        if (status)
            Console.WriteLine("Successfully added a bike.");

        var value = db.StringGet("bike:1");

        if (value.HasValue)
            Console.WriteLine("The name of the bike is: " + value + ".");

    }
}

```

##### Go

```go
package example_commands_test

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/v9"
)

func ExampleClient_Set_and_get() {
	ctx := context.Background()

	rdb := redis.NewClient(&redis.Options{
		Addr:     "localhost:6379",
		Password: "", // no password docs
		DB:       0,  // use default DB
	})



	err := rdb.Set(ctx, "bike:1", "Process 134", 0).Err()
	if err != nil {
		panic(err)
	}

	fmt.Println("OK")

	value, err := rdb.Get(ctx, "bike:1").Result()
	if err != nil {
		panic(err)
	}
	fmt.Printf("The name of the bike is %s", value)

}


```

##### Java (Asynchronous - Lettuce)

```java
package io.redis.examples.async;

import io.lettuce.core.RedisClient;
import io.lettuce.core.api.async.RedisAsyncCommands;
import io.lettuce.core.api.StatefulRedisConnection;

import java.util.concurrent.CompletableFuture;

public class SetGetExample {

    public void run() {
        RedisClient redisClient = RedisClient.create("redis://localhost:6379");

        try (StatefulRedisConnection<String, String> connection = redisClient.connect()) {
            RedisAsyncCommands<String, String> asyncCommands = connection.async();


            CompletableFuture<Void> setGetExample = asyncCommands.set("bike:1", "Process 134")
                    .thenCompose(res1 -> {
                        System.out.println(res1); // >>> OK
                        return asyncCommands.get("bike:1");
                    }).thenAccept(res2 -> {
                        System.out.println(res2); // >>> Process 134
                    }).toCompletableFuture();

            setGetExample.join();
        } finally {
            redisClient.shutdown();
        }
    }
}

```

##### Java (Reactive - Lettuce)

```java
package io.redis.examples.reactive;

import io.lettuce.core.RedisClient;
import io.lettuce.core.api.reactive.RedisReactiveCommands;
import io.lettuce.core.api.StatefulRedisConnection;

import reactor.core.publisher.Mono;

public class SetGetExample {

    public void run() {
        RedisClient redisClient = RedisClient.create("redis://localhost:6379");

        try (StatefulRedisConnection<String, String> connection = redisClient.connect()) {
            RedisReactiveCommands<String, String> reactiveCommands = connection.reactive();


            Mono<Void> setGetExample = reactiveCommands.set("bike:1", "Process 134")
                    .doOnNext(res1 -> {
                        System.out.println(res1); // >>> OK
                    })
                    .then(reactiveCommands.get("bike:1"))
                    .doOnNext(res2 -> {
                        System.out.println(res2); // >>> Process 134
                    })
                    .then();

            setGetExample.block();
        } finally {
            redisClient.shutdown();
        }
    }
}

```

##### Java (Synchronous - Jedis)

```java
package io.redis.examples;

import redis.clients.jedis.RedisClient;


public class SetGetExample {

  public void run() {

    RedisClient jedis = RedisClient.create("redis://localhost:6379");

    String status = jedis.set("bike:1", "Process 134");

    if ("OK".equals(status)) System.out.println("Successfully added a bike.");

    String value = jedis.get("bike:1");

    if (value != null) System.out.println("The name of the bike is: " + value + ".");


    jedis.close();
  }
}

```

##### JavaScript (Node.js)

```javascript

import { createClient } from 'redis';

const client = createClient();

client.on('error', err => console.log('Redis Client Error', err));

await client.connect().catch(console.error);

await client.set('bike:1', 'Process 134');
const value = await client.get('bike:1');
console.log(value);
// returns 'Process 134'

await client.close();

```

##### JavaScript (Node.js)

```javascript

import assert from 'node:assert';
import { Redis } from 'ioredis';

const redis = new Redis();


const res1 = await redis.set('bike:1', 'Process 134');
console.log(res1); // >>> OK

const res2 = await redis.get('bike:1');
console.log(res2); // >>> Process 134


redis.disconnect();

```

##### JavaScript (Node.js)

```javascript

import { createClient } from 'redis';

const client = createClient();

client.on('error', err => console.log('Redis Client Error', err));

await client.connect().catch(console.error);

await client.set('bike:1', 'Process 134');
const value = await client.get('bike:1');
console.log(value);
// returns 'Process 134'

await client.close();

```

##### JavaScript (Node.js)

```javascript

import assert from 'node:assert';
import { Redis } from 'ioredis';

const redis = new Redis();


const res1 = await redis.set('bike:1', 'Process 134');
console.log(res1); // >>> OK

const res2 = await redis.get('bike:1');
console.log(res2); // >>> Process 134


redis.disconnect();

```

##### PHP

```php
<?php
use Predis\Client as PredisClient;

class SetGetTest
{
    public function testSetGet() {
        $r = new PredisClient([
            'scheme'   => 'tcp',
            'host'     => '127.0.0.1',
            'port'     => 6379,
            'password' => '',
            'database' => 0,
        ]);


        $res1 = $r->set('bike:1', 'Process 134');
        echo $res1 . PHP_EOL; // >>> OK

        $res2 = $r->get('bike:1');
        echo $res2 . PHP_EOL; // >>> Process 134

    }
}

```

##### Python

```python
"""
Code samples for data structure store quickstart pages:
    https://redis.io/docs/latest/develop/get-started/data-store/
"""

import redis

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

res = r.set("bike:1", "Process 134")
print(res)
# >>> True

res = r.get("bike:1")
print(res)
# >>> "Process 134"

```

##### Ruby

```ruby
require 'redis'

r = Redis.new


res1 = r.set('bike:1', 'Process 134')
puts res1 # >>> OK

res2 = r.get('bike:1')
puts res2 # >>> Process 134


```

##### Rust (Asynchronous)

```rust
mod set_and_get_tests {
    use redis::AsyncCommands;

    async fn run() {
        let mut r = match redis::Client::open("redis://127.0.0.1") {
            Ok(client) => match client.get_multiplexed_async_connection().await {
                Ok(conn) => conn,
                Err(e) => {
                    println!("Failed to connect to Redis: {e}");
                    return;
                }
            },
            Err(e) => {
                println!("Failed to create Redis client: {e}");
                return;
            }
        };


        if let Ok(res1) = r.set("bike:1", "Process 134").await {
            let res1: String = res1;
            println!("{res1}"); // >>> OK
        }

        if let Ok(res2) = r.get("bike:1").await {
            let res2: String = res2;
            println!("{res2}"); // >>> Process 134
        }

    }
}

```

##### Rust (Synchronous)

```rust
mod set_and_get_tests {
    use redis::Commands;

    fn run() {
        let mut r = match redis::Client::open("redis://127.0.0.1") {
            Ok(client) => match client.get_connection() {
                Ok(conn) => conn,
                Err(e) => {
                    println!("Failed to connect to Redis: {e}");
                    return;
                }
            },
            Err(e) => {
                println!("Failed to create Redis client: {e}");
                return;
            }
        };


        if let Ok(res1) = r.set("bike:1", "Process 134") {
            let res1: String = res1;
            println!("{res1}"); // >>> OK
        }

        if let Ok(res2) = r.get("bike:1") {
            let res2: String = res2;
            println!("{res2}"); // >>> Process 134
        }

    }
}

```



## Details

### Hash digest

A hash digest is a fixed-size numerical representation of a string value, computed using the XXH3 hash algorithm. Redis uses this hash digest for efficient comparison operations without needing to compare the full string content. You can retrieve a key's hash digest using the [`DIGEST`](https://redis.io/docs/latest/commands/digest) command, which returns it as a hexadecimal string that you can use with the `IFDEQ` and `IFDNE` options, and also the [`DELEX`](https://redis.io/docs/latest/commands/delex) command's `IFDEQ` and `IFDNE` options.

### Patterns

Note: The following pattern is discouraged in favor of [the Redlock algorithm](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks) which is only a bit more complex to implement, but offers better guarantees and is fault tolerant.

The command `SET resource-name anystring NX EX max-lock-time` is a simple way to implement a locking system with Redis.

A client can acquire the lock if the above command returns `OK` (or retry after some time if the command returns Nil), and remove the lock just using [`DEL`](https://redis.io/docs/latest/commands/del).

The lock will be auto-released after the expire time is reached.

It is possible to make this system more robust modifying the unlock schema as follows:

* Instead of setting a fixed string, set a non-guessable large random string, called token.
* Instead of releasing the lock with [`DEL`](https://redis.io/docs/latest/commands/del), send a script that only removes the key if the value matches.

This avoids that a client will try to release the lock after the expire time deleting the key created by another client that acquired the lock later.

An example of unlock script would be similar to the following:

    if redis.call("get",KEYS[1]) == ARGV[1]
    then
        return redis.call("del",KEYS[1])
    else
        return 0
    end

The script should be called with `EVAL ...script... 1 resource-name token-value`

## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

* If `GET` was not specified, one of the following:
  * [Null bulk string reply](../../develop/reference/protocol-spec#bulk-strings) in the following two cases.
    * The key doesn’t exist and `XX/IFEQ/IFDEQ` was specified. The key was not created.
    * The key exists, and `NX` was specified or a specified `IFEQ/IFNE/IFDEQ/IFDNE` condition is false. The key was not set.
  * [Simple string reply](../../develop/reference/protocol-spec#simple-strings): `OK`: The key was set.
* If `GET` was specified, one of the following:
  * [Null bulk string reply](../../develop/reference/protocol-spec#bulk-strings): The key didn't exist before the `SET` operation, whether the key was created of not.
  * [Bulk string reply](../../develop/reference/protocol-spec#bulk-strings): The previous value of the key, whether the key was set or not.

**RESP3:**

* If `GET` was not specified, one of the following:
  * [Null reply](../../develop/reference/protocol-spec#nulls) in the following two cases.
    * The key doesn’t exist and `XX/IFEQ/IFDEQ` was specified. The key was not created.
    * The key exists, and `NX` was specified or a specified `IFEQ/IFNE/IFDEQ/IFDNE` condition is false. The key was not set.
  * [Simple string reply](../../develop/reference/protocol-spec#simple-strings): `OK`: The key was set.
* If `GET` was specified, one of the following:
  * [Null reply](../../develop/reference/protocol-spec#nulls): The key didn't exist before the `SET` operation, whether the key was created of not.
  * [Bulk string reply](../../develop/reference/protocol-spec#bulk-strings): The previous value of the key, whether the key was set or not.



