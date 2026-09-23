# HSET

```json metadata
{
  "schema_version": 2,
  "title": "HSET",
  "description": "Creates or modifies the value of a field in a hash.",
  "categories": ["docs","develop","stack","oss","rs","rc","oss","kubernetes","clients"],
  "arguments": [{"display_text":"key","key_spec_index":0,"name":"key","type":"key"},{"arguments":[{"display_text":"field","name":"field","type":"string"},{"display_text":"value","name":"value","type":"string"}],"multiple":true,"name":"data","type":"block"}],
  "syntax_fmt": "HSET key field value [field value ...]",
  "complexity": "O(1) for each field/value pair added, so O(N) to add N field/value pairs when the command is called with multiple field/value pairs.",
  "group": "hash",
  "command_flags": ["write","denyoom","fast"],
  "acl_categories": ["@write","@hash","@fast"],
  "since": "2.0.0",
  "arity": -4,
  "key_specs": [{"RW":true,"begin_search":{"spec":{"index":1},"type":"index"},"find_keys":{"spec":{"keystep":1,"lastkey":0,"limit":0},"type":"range"},"update":true}],
  "tableOfContents": {"sections":[{"id":"required-arguments","title":"Required arguments"},{"id":"examples","title":"Examples"},{"id":"redis-software-and-redis-cloud-compatibility","title":"Redis Software and Redis Cloud compatibility"},{"id":"return-information","title":"Return information"}]}

,
  "codeExamples": [{"codetabsId":"cmds_hash-stephset","commands":[{"acl_categories":["@write","@hash","@fast"],"complexity":"O(1)","name":"HSET"},{"acl_categories":["@read","@hash","@fast"],"complexity":"O(1)","name":"HGET"},{"acl_categories":["@read","@hash","@slow"],"complexity":"O(N)","name":"HGETALL"}],"description":"Foundational: Set one or more field-value pairs in a hash using HSET (creates hash if needed, overwrites existing fields, returns count of new fields)","difficulty":"beginner","id":"hset","languages":[{"id":"redis-cli","panelId":"panel_redis-cli_cmds_hash-stephset"},{"clientId":"redis-py","clientName":"redis-py","id":"Python","langId":"python","panelId":"panel_Python_cmds_hash-stephset"},{"id":"Node-js","panelId":"panel_Nodejs_cmds_hash-stephset"},{"clientId":"ioredis","clientName":"ioredis","id":"ioredis","langId":"javascript","panelId":"panel_ioredis_cmds_hash-stephset"},{"clientId":"jedis","clientName":"Jedis","id":"Java-Sync","langId":"java","panelId":"panel_Java-Sync_cmds_hash-stephset"},{"clientId":"lettuce","clientName":"Lettuce","id":"Lettuce-Sync","langId":"java","panelId":"panel_Lettuce-Sync_cmds_hash-stephset"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Async","langId":"java","panelId":"panel_Java-Async_cmds_hash-stephset"},{"clientId":"lettuce","clientName":"Lettuce","id":"Java-Reactive","langId":"java","panelId":"panel_Java-Reactive_cmds_hash-stephset"},{"clientId":"go-redis","clientName":"go-redis","id":"Go","langId":"go","panelId":"panel_Go_cmds_hash-stephset"},{"clientId":"hiredis","clientName":"hiredis","id":"C","langId":"c","panelId":"panel_C_cmds_hash-stephset"},{"id":"dotnet-Sync (SE-Redis)","panelId":"panel_Csharp-Sync (SERedis)_cmds_hash-stephset"},{"clientId":"predis","clientName":"Predis","id":"PHP","langId":"php","panelId":"panel_PHP_cmds_hash-stephset"},{"clientId":"redis-rb","clientName":"redis-rb","id":"Ruby","langId":"ruby","panelId":"panel_Ruby_cmds_hash-stephset"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Sync","langId":"rust","panelId":"panel_Rust-Sync_cmds_hash-stephset"},{"clientId":"redis-rs","clientName":"redis-rs","id":"Rust-Async","langId":"rust","panelId":"panel_Rust-Async_cmds_hash-stephset"}]}]
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

Sets the specified fields to their respective values in the hash stored at `key`.

This command overwrites the values of specified fields that exist in the hash.
If `key` doesn't exist, a new key holding a hash is created.

## Required arguments

<details open><summary><code>key</code></summary>

The name of the key that holds the hash.

</details>

<details open><summary><code>field value [field value ...]</code></summary>

One or more field-value pairs to set in the hash.

</details>

## Examples

Foundational: Set one or more field-value pairs in a hash using HSET (creates hash if needed, overwrites existing fields, returns count of new fields)

**Difficulty:** Beginner

**Commands:** HSET, HGET, HGETALL

**Complexity:**
- HSET: O(1)
- HGET: O(1)
- HGETALL: O(N)

**Available in:** Redis CLI, C, C#, Go, Java, Java (Asynchronous - Lettuce), Java (Reactive - Lettuce), Java (Synchronous - Jedis), JavaScript (Node.js), JavaScript (Node.js), PHP, Python, Ruby, Rust (Asynchronous), Rust (Synchronous)

##### Redis CLI

```
> HSET myhash field1 "Hello"
(integer) 1
> HGET myhash field1
"Hello"
> HSET myhash field2 "Hi" field3 "World"
(integer) 2
> HGET myhash field2
"Hi"
> HGET myhash field3
"World"
> HGETALL myhash
1) "field1"
2) "Hello"
3) "field2"
4) "Hi"
5) "field3"
6) "World"
```

##### C

```c
    reply = redisCommand(c, "HSET myhash field1 Hello");
    printf("%lld\n", reply->integer);
    // >>> 1
    freeReplyObject(reply);

    reply = redisCommand(c, "HGET myhash field1");
    printf("%s\n", reply->str);
    // >>> Hello
    freeReplyObject(reply);

    reply = redisCommand(c, "HSET myhash field2 Hi field3 World");
    printf("%lld\n", reply->integer);
    // >>> 2
    freeReplyObject(reply);

    reply = redisCommand(c, "HGET myhash field2");
    printf("%s\n", reply->str);
    // >>> Hi
    freeReplyObject(reply);

    reply = redisCommand(c, "HGET myhash field3");
    printf("%s\n", reply->str);
    // >>> World
    freeReplyObject(reply);

    // HGETALL returns field and value alternating in a flat array.
    reply = redisCommand(c, "HGETALL myhash");
    for (size_t i = 0; i < reply->elements; i += 2) {
        printf("%s=%s\n", reply->element[i]->str, reply->element[i + 1]->str);
    }
    // >>> field1=Hello
    // >>> field2=Hi
    // >>> field3=World
    freeReplyObject(reply);
```

##### C#

```csharp
        bool hsetRes1 = db.HashSet("myhash", "field1", "Hello");
        RedisValue hsetRes2 = db.HashGet("myhash", "field1");
        Console.WriteLine(hsetRes2);    // >>> Hello

        db.HashSet(
            "myhash",
            [
                new("field2", "Hi"),
                new("field3", "World")
            ]
        );

        RedisValue hsetRes3 = db.HashGet("myhash", "field2");
        Console.WriteLine(hsetRes3);    // >>> Hi

        RedisValue hsetRes4 = db.HashGet("myhash", "field3");
        Console.WriteLine(hsetRes4);    // >>> World

        HashEntry[] hsetRes5 = db.HashGetAll("myhash");
        Console.WriteLine($"{string.Join(", ", hsetRes5.Select(h => $"{h.Name}: {h.Value}"))}");
        // >>> field1: Hello, field2: Hi, field3: World
```

##### Go

```go
	res1, err := rdb.HSet(ctx, "myhash", "field1", "Hello").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(res1) // >>> 1

	res2, err := rdb.HGet(ctx, "myhash", "field1").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(res2) // >>> Hello

	res3, err := rdb.HSet(ctx, "myhash",
		"field2", "Hi",
		"field3", "World",
	).Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(res3) // >>> 2

	res4, err := rdb.HGet(ctx, "myhash", "field2").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(res4) // >>> Hi

	res5, err := rdb.HGet(ctx, "myhash", "field3").Result()

	if err != nil {
		panic(err)
	}

	fmt.Println(res5) // >>> World

	res6, err := rdb.HGetAll(ctx, "myhash").Result()

	if err != nil {
		panic(err)
	}

	keys := make([]string, 0, len(res6))

	for key, _ := range res6 {
		keys = append(keys, key)
	}

	sort.Strings(keys)

	for _, key := range keys {
		fmt.Printf("Key: %v, value: %v\n", key, res6[key])
	}
	// >>> Key: field1, value: Hello
	// >>> Key: field2, value: Hi
	// >>> Key: field3, value: World
```

##### Java

```java
            // `hset` returns true because `field1` is a new field.
            boolean res4 = syncCommands.hset("myhash", "field1", "Hello");
            System.out.println(res4); // >>> true

            String res5 = syncCommands.hget("myhash", "field1");
            System.out.println(res5); // >>> Hello

            // The `Map` overload of `hset` returns the number of new fields.
            Map<String, String> newFields = new HashMap<>();
            newFields.put("field2", "Hi");
            newFields.put("field3", "World");

            Long res6 = syncCommands.hset("myhash", newFields);
            System.out.println(res6); // >>> 2

            String res7 = syncCommands.hget("myhash", "field2");
            System.out.println(res7); // >>> Hi

            String res8 = syncCommands.hget("myhash", "field3");
            System.out.println(res8); // >>> World

            // `hgetall` returns a `Map`, whose iteration order isn't
            // guaranteed. Wrap it in a `TreeMap` to sort the fields by name.
            Map<String, String> res9 = syncCommands.hgetall("myhash");
            System.out.println(new TreeMap<>(res9));
            // >>> {field1=Hello, field2=Hi, field3=World}
```

##### Java (Asynchronous - Lettuce)

```java
            CompletableFuture<Void> hSetExample = asyncCommands.hset("myhash", "field1", "Hello").thenCompose(res1 -> {
                System.out.println(res1); // >>> 1
                return asyncCommands.hget("myhash", "field1");
            }).thenCompose(res2 -> {
                System.out.println(res2); // >>> Hello
                Map<String, String> hSetExampleParams = new HashMap<>();
                hSetExampleParams.put("field2", "Hi");
                hSetExampleParams.put("field3", "World");
                return asyncCommands.hset("myhash", hSetExampleParams);
            }).thenCompose(res3 -> {
                System.out.println(res3); // >>> 2
                return asyncCommands.hget("myhash", "field2");
            }).thenCompose(res4 -> {
                System.out.println(res4); // >>> Hi
                return asyncCommands.hget("myhash", "field3");
            }).thenCompose(res5 -> {
                System.out.println(res5); // >>> World
                return asyncCommands.hgetall("myhash");
            }).thenAccept(res6 -> {
                System.out.println(res6);
                // >>> {field1=Hello, field2=Hi, field3=World}
            }).toCompletableFuture();
```

##### Java (Reactive - Lettuce)

```java
            Mono<Boolean> hSetExample1 = reactiveCommands.hset("myhash", "field1", "Hello").doOnNext(result -> {
                System.out.println(result); // >>> True
            });

            hSetExample1.block();

            Mono<String> hSetExample2 = reactiveCommands.hget("myhash", "field1").doOnNext(result -> {
                System.out.println(result); // >>> Hello
            });

            hSetExample2.block();

            Map<String, String> hSetExampleParams = new HashMap<>();
            hSetExampleParams.put("field2", "Hi");
            hSetExampleParams.put("field3", "World");

            Mono<Long> hSetExample3 = reactiveCommands.hset("myhash", hSetExampleParams).doOnNext(result -> {
                System.out.println(result); // >>> 2
            });

            hSetExample3.block();

            Mono<String> hSetExample4 = reactiveCommands.hget("myhash", "field2").doOnNext(result -> {
                System.out.println(result); // >>> Hi
            });

            hSetExample4.block();

            Mono<String> hSetExample5 = reactiveCommands.hget("myhash", "field3").doOnNext(result -> {
                System.out.println(result); // >>> World
            });

            hSetExample5.block();

            Mono<Map<String, String>> hSetExample6 = reactiveCommands.hgetall("myhash").collectMap(
                    KeyValue::getKey, KeyValue::getValue
            ).doOnNext(result -> {
                System.out.println(result);
                // >>> {field1=Hello, field2=Hi, field3=World}
            });
```

##### Java (Synchronous - Jedis)

```java
        Map<String, String> hSetExampleParams = new HashMap<>();
        hSetExampleParams.put("field1", "Hello");
        long hSetResult1 = jedis.hset("myhash", hSetExampleParams);
        System.out.println(hSetResult1);    // >>> 1

        String hSetResult2 = jedis.hget("myhash", "field1");
        System.out.println(hSetResult2);    // >>> Hello

        hSetExampleParams.clear();
        hSetExampleParams.put("field2", "Hi");
        hSetExampleParams.put("field3", "World");
        long hSetResult3 = jedis.hset("myhash",hSetExampleParams);
        System.out.println(hSetResult3);    // >>> 2

        String hSetResult4 = jedis.hget("myhash", "field2");
        System.out.println(hSetResult4);    // >>> Hi

        String hSetResult5 = jedis.hget("myhash", "field3");
        System.out.println(hSetResult5);    // >>> World

        Map<String, String> hSetResult6 = jedis.hgetAll("myhash");
        
        for (String key: hSetResult6.keySet()) {
            System.out.println("Key: " + key + ", Value: " + hSetResult6.get(key));
        }
        // >>> Key: field3, Value: World
        // >>> Key: field2, Value: Hi
        // >>> Key: field1, Value: Hello
```

##### JavaScript (Node.js)

```javascript
const res1 = await client.hSet('myhash', 'field1', 'Hello')
console.log(res1) // 1

const res2 = await client.hGet('myhash', 'field1')
console.log(res2) // Hello

const res3 = await client.hSet(
  'myhash',
  {
    'field2': 'Hi',
    'field3': 'World'
  }
)
console.log(res3) // 2

const res4 = await client.hGet('myhash', 'field2')
console.log(res4) // Hi

const res5 = await client.hGet('myhash', 'field3')
console.log(res5) // World

const res6 = await client.hGetAll('myhash')
console.log(res6)  

```

##### JavaScript (Node.js)

```javascript
const hsetRes1 = await redis.hset('myhash', 'field1', 'Hello');
console.log(hsetRes1); // >>> 1

console.log(await redis.hget('myhash', 'field1')); // >>> Hello

const hsetRes2 = await redis.hset('myhash', { field2: 'Hi', field3: 'World' });
console.log(hsetRes2); // >>> 2

console.log(await redis.hget('myhash', 'field2')); // >>> Hi
console.log(await redis.hget('myhash', 'field3')); // >>> World

const hsetRes3 = await redis.hgetall('myhash');
console.log(hsetRes3);
// >>> { field1: 'Hello', field2: 'Hi', field3: 'World' }
```

##### JavaScript (Node.js)

```javascript
const res1 = await client.hSet('myhash', 'field1', 'Hello')
console.log(res1) // 1

const res2 = await client.hGet('myhash', 'field1')
console.log(res2) // Hello

const res3 = await client.hSet(
  'myhash',
  {
    'field2': 'Hi',
    'field3': 'World'
  }
)
console.log(res3) // 2

const res4 = await client.hGet('myhash', 'field2')
console.log(res4) // Hi

const res5 = await client.hGet('myhash', 'field3')
console.log(res5) // World

const res6 = await client.hGetAll('myhash')
console.log(res6)  

```

##### JavaScript (Node.js)

```javascript
const hsetRes1 = await redis.hset('myhash', 'field1', 'Hello');
console.log(hsetRes1); // >>> 1

console.log(await redis.hget('myhash', 'field1')); // >>> Hello

const hsetRes2 = await redis.hset('myhash', { field2: 'Hi', field3: 'World' });
console.log(hsetRes2); // >>> 2

console.log(await redis.hget('myhash', 'field2')); // >>> Hi
console.log(await redis.hget('myhash', 'field3')); // >>> World

const hsetRes3 = await redis.hgetall('myhash');
console.log(hsetRes3);
// >>> { field1: 'Hello', field2: 'Hi', field3: 'World' }
```

##### PHP

```php

        $hSetResult1 = $this->redis->hset('myhash', 'field1', 'Hello');
        echo "HSET myhash field1 Hello: " . $hSetResult1 . "\n"; // >>> 1

        $hSetResult2 = $this->redis->hget('myhash', 'field1');
        echo "HGET myhash field1: " . $hSetResult2 . "\n"; // >>> Hello

        $hSetResult3 = $this->redis->hmset('myhash', ['field2' => 'Hi', 'field3' => 'World']);
        echo "HMSET myhash field2 Hi field3 World: " . ($hSetResult3 ? 'OK' : 'FAIL') . "\n"; // >>> OK

        $hSetResult4 = $this->redis->hget('myhash', 'field2');
        echo "HGET myhash field2: " . $hSetResult4 . "\n"; // >>> Hi

        $hSetResult5 = $this->redis->hget('myhash', 'field3');
        echo "HGET myhash field3: " . $hSetResult5 . "\n"; // >>> World

        $hSetResult6 = $this->redis->hgetall('myhash');
        echo "HGETALL myhash: ";
        foreach ($hSetResult6 as $key => $value) {
            echo "Key: $key, Value: $value; ";
        }
        echo "\n";
```

##### Python

```python
res1 = r.hset("myhash", "field1", "Hello")
print(res1)
# >>> 1

res2 = r.hget("myhash", "field1")
print(res2)
# >>> Hello

res3 = r.hset("myhash", mapping={"field2": "Hi", "field3": "World"})
print(res3)
# >>> 2

res4 = r.hget("myhash", "field2")
print(res4)
# >>> Hi

res5 = r.hget("myhash", "field3")
print(res5)
# >>> World

res6 = r.hgetall("myhash")
print(res6)
# >>> { "field1": "Hello", "field2": "Hi", "field3": "World" }

```

##### Ruby

```ruby
res4 = r.hset('myhash', 'field1', 'Hello')
puts res4 # >>> 1

res5 = r.hget('myhash', 'field1')
puts res5 # >>> Hello

res6 = r.hset('myhash', { 'field2' => 'Hi', 'field3' => 'World' })
puts res6 # >>> 2

res7 = r.hget('myhash', 'field2')
puts res7 # >>> Hi

res8 = r.hget('myhash', 'field3')
puts res8 # >>> World

res9 = r.hgetall('myhash')
puts res9.inspect
# >>> {"field1"=>"Hello", "field2"=>"Hi", "field3"=>"World"}
```

##### Rust (Asynchronous)

```rust
        match r.hset("myhash", "field1", "Hello").await {
            Ok(res1) => {
                let res1: i32 = res1;
                println!("{res1}");    // >>> 1
            },
            Err(e) => {
                println!("Error setting hash field: {e}");
                return;
            }
        }

        match r.hget("myhash", "field1").await {
            Ok(res2) => {
                let res2: Option<String> = res2;
                match res2 {
                    Some(value) => {
                        println!("{value}");    // >>> Hello
                    },
                    None => {
                        println!("None");
                    }
                }
            },
            Err(e) => {
                println!("Error getting hash field: {e}");
                return;
            }
        }

        // Set multiple fields using hset_multiple
        let hash_fields = [
            ("field2", "Hi"),
            ("field3", "World"),
        ];

        if let Ok(res) = r.hset_multiple("myhash", &hash_fields).await {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.hget("myhash", "field2").await {
            Ok(res4) => {
                let res4: Option<String> = res4;
                match res4 {
                    Some(value) => {
                        println!("{value}");    // >>> Hi
                    },
                    None => {
                        println!("None");
                    }
                }
            },
            Err(e) => {
                println!("Error getting hash field: {e}");
                return;
            }
        }

        match r.hget("myhash", "field3").await {
            Ok(res5) => {
                let res5: Option<String> = res5;
                match res5 {
                    Some(value) => {
                        println!("{value}");    // >>> World
                    },
                    None => {
                        println!("None");
                    }
                }
            },
            Err(e) => {
                println!("Error getting hash field: {e}");
                return;
            }
        }

        match r.hgetall("myhash").await {
            Ok(res6) => {
                let res6: HashMap<String, String> = res6;
                println!("{res6:?}");    // >>> {"field1": "Hello", "field2": "Hi", "field3": "World"}
            },
            Err(e) => {
                println!("Error getting all hash fields: {e}");
                return;
            }
        }

```

##### Rust (Synchronous)

```rust
        match r.hset("myhash", "field1", "Hello") {
            Ok(res1) => {
                let res1: i32 = res1;
                println!("{res1}");    // >>> 1
            },
            Err(e) => {
                println!("Error setting hash field: {e}");
                return;
            }
        }

        match r.hget("myhash", "field1") {
            Ok(res2) => {
                let res2: Option<String> = res2;
                match res2 {
                    Some(value) => {
                        println!("{value}");    // >>> Hello
                    },
                    None => {
                        println!("None");
                    }
                }
            },
            Err(e) => {
                println!("Error getting hash field: {e}");
                return;
            }
        }

        // Set multiple fields using hset_multiple
        let hash_fields = [
            ("field2", "Hi"),
            ("field3", "World"),
        ];

        if let Ok(res) = r.hset_multiple("myhash", &hash_fields) {
            let res: String = res;
            println!("{res}");    // >>> OK
        }

        match r.hget("myhash", "field2") {
            Ok(res4) => {
                let res4: Option<String> = res4;
                match res4 {
                    Some(value) => {
                        println!("{value}");    // >>> Hi
                    },
                    None => {
                        println!("None");
                    }
                }
            },
            Err(e) => {
                println!("Error getting hash field: {e}");
                return;
            }
        }

        match r.hget("myhash", "field3") {
            Ok(res5) => {
                let res5: Option<String> = res5;
                match res5 {
                    Some(value) => {
                        println!("{value}");    // >>> World
                    },
                    None => {
                        println!("None");
                    }
                }
            },
            Err(e) => {
                println!("Error getting hash field: {e}");
                return;
            }
        }

        match r.hgetall("myhash") {
            Ok(res6) => {
                let res6: HashMap<String, String> = res6;
                println!("{res6:?}");    // >>> {"field1": "Hello", "field2": "Hi", "field3": "World"}
            },
            Err(e) => {
                println!("Error getting all hash fields: {e}");
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

[Integer reply](../../develop/reference/protocol-spec#integers): the number of fields that were added.

**RESP3:**

[Integer reply](../../develop/reference/protocol-spec#integers): the number of fields that were added.



