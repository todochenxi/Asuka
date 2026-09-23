# GET

```json metadata
{
  "schema_version": 2,
  "title": "GET",
  "description": "Returns the string value of a key.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"}],
  "syntax_fmt": "GET key",
  "complexity": "O(1)",
  "group": "string",
  "command_flags": ["readonly","fast"],
  "acl_categories": ["@read","@string","@fast"],
  "since": "1.0.0",
  "arity": 2,
  "key_specs": [{"RO":true,"access":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"}}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"set_and_get-stepget","commands":[{"acl_categories":["@read","@string","@fast"],"complexity":"O(1)","name":"GET"},{"acl_categories":["@write","@string","@slow"],"complexity":"O(1)","name":"SET"}],"description":"Foundational: Retrieve the string value of a key using GET (returns nil if key doesn\u0026amp;#39;t exist)","difficulty":"beginner","id":"get","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_set_and_get-stepget"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_set_and_get-stepget"},{"id":"Node-js","panelId":"panel_Nodejs_set_and_get-stepget"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_set_and_get-stepget"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_set_and_get-stepget"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_set_and_get-stepget"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_set_and_get-stepget"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_set_and_get-stepget"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_set_and_get-stepget"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_set_and_get-stepget"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_set_and_get-stepget"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_set_and_get-stepget"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_set_and_get-stepget"}]}]
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

Get the value of `key`.
If the key does not exist, `nil` is returned.
An error is returned if the value stored at `key` is not a string, because `GET`
only handles string values.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key.

</details>

## Examples

Foundational: Retrieve the string value of a key using GET (returns nil if key doesn't exist)

**Difficulty:** Beginner

**Commands:** GET, SET

**Complexity:**
- GET: O(1)
- SET: O(1)

**Available in:** Redis CLI, C#, Go, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> GET nonexisting
(nil)
> SET mykey "Hello"
OK
> GET mykey
"Hello"
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



## Redis Software and Redis Cloud compatibility

| Redis<br />Software | Redis<br />Cloud | <span style="min-width: 9em; display: table-cell">Notes</span> |
|:----------------------|:-----------------|:------|
| <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> | <span title="Supported">&#x2705; Standard</span><br /><span title="Supported"><nobr>&#x2705; Active-Active</nobr></span> |  |

## Return information

**RESP2:**

One of the following:
* [Bulk string reply](../../develop/reference/protocol-spec#bulk-strings): the value of the key.
* [Nil reply](../../develop/reference/protocol-spec#bulk-strings): if the key does not exist.

**RESP3:**

One of the following:
* [Bulk string reply](../../develop/reference/protocol-spec#bulk-strings): the value of the key.
* [Null reply](../../develop/reference/protocol-spec#nulls): key does not exist.



