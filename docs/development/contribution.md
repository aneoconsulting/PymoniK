# Contributing

This doesn't differ from our other projects ([Read ArmoniK.CLI's CONTRIBUTING.md](https://github.com/aneoconsulting/ArmoniK.CLI/blob/main/CONTRIBUTING.md)).

## Open Issues:

Here's a non-exhaustive list of things that are outright/partially missing from PymoniK that we'd like to see added in: 

- **Unit tests, end-to-end tests** : This project doesn't have any testing associated to it, 0% code coverage. We'd like to change this.
- **More sophisticated examples** : We'd like to add even more examples and tutorials of common use cases that use ArmoniK under the hood.
- **PymoniK logger** : The session created/closed/cancelled prints and the different errors should be logged instead of being printed.
- **Local PymoniK** : Once of the big advantages of the way things have been coded is being able to switch from/to a remote/local context by just removing the `invoke` methods. This could be done even better, by adding a `local=True` flag to PymoniK that makes it so invokes are executed as regular function calls that run locally. The challenge is mainly handling `Pymonik.put`s and the map_invoke. 
- **Rename `ResultHandle` to `ObjectHandle`** : The naming of `ResultHandle` was choosen because invocations return results, but as it turns out, these results are also then served as inputs. It'd be better for naming (especially since you can put things onto ArmoniK) to rename `ResultHandle` to the more generic `ObjectHandle`.
- **Cleaner sub-tasking** : Subtasking requires the user to pass in a `delegate=True` flag to invokes, this isn't particularly clean or nice. There must be a better way of doing it. 
- **Tasks returning multiple results**: As of right now, a task can only return a single result object (even when you return a tuple). There should be support for cases where you'd like to return multiple results from a task and not have them be grouped up into one (multiple smaller objects being passed onto multiple tasks). We think this change would be easier to implement once sub-tasking is in place, since it also involves analyzing what the user will return. There should be pre-execution tests to check if the user is returning a different number of results in different branches of the task and that should result in a failure (invalid task). 
- **`results.as_completed`** : For more sophisticated and better time-to-execute, we'd like to implement a method for `MultiResultHandle` that allows the user to for instance loop through a `MultiResultHandle` and have the code execute as the result is done/retrieved. Moreover, as a side-effect, having this feature would allow for the usage of `tqdm` to create progress bars which is also really nice. 
- **Intermediate Objects** : Support being able to create/download ArmoniK Objects (Intermediate results) within tasks. The download part would require `GetDirectData` in the Python API.   
- **Remote to local error propagation** : Supply an additional "error name" to created tasks, when a result creation fails, we create this result, locally we can retrieve the remote stack trace using `my_result.error()` if we try-except, either that are we enrich the local result failure grpc exception with the remote one. 
- **Test JIT-ing** : jitting tasks using namba/taichi if they're pure for additional performance. (Should just test if it'd work as intended..)
- **More sophisticated result deserialization**: Right now it's just first-level depickling, it'd be nice to be able to pass in a dict that has ResultHandle values for example and be able to dynamically fetch those but that'd add a lot of complexity to data dependencies that'd need to be handled. There has to be a nice way of doing this and it's worth exploring
- **PymoniK Visualizer**: With the current implementation of invoke/map_invoke, we can make it so you can surround your PymoniK context with a Grapher context that dynamically builds up a visualization of your task graph that you can then save and look at/analyze/vizualize later.  
