import grpc
import cloudpickle as pickle

from typing import Callable, Optional, Union
from armonik.common.channel import create_channel
from .core import Task, P_Args, R_Type


def create_grpc_channel(
    endpoint: str,
    certificate_authority: Optional[str] = None,
    client_certificate: Optional[str] = None,
    client_key: Optional[str] = None,
) -> grpc.Channel:
    """
    Create a gRPC channel based on the configuration.
    """
    cleaner_endpoint = endpoint
    if cleaner_endpoint.startswith("http://"):
        cleaner_endpoint = cleaner_endpoint[7:]
    if cleaner_endpoint.endswith("/"):
        cleaner_endpoint = cleaner_endpoint[:-1]
    if certificate_authority:
        # Create grpc channel with tls
        channel = create_channel(
            cleaner_endpoint,
            options=(("grpc.ssl_target_name_override", "armonik.local"),),
            certificate_authority=certificate_authority,
            client_certificate=client_certificate,
            client_key=client_key,
        )
    else:
        # Create insecure grpc channel
        channel = grpc.insecure_channel(cleaner_endpoint)
    return channel


class LazyArgs:
    def __init__(self, args_to_pickle):
        # We store the *pickled* representation of the arguments, not the arguments themselves.
        self.pickled_args = pickle.dumps(args_to_pickle)  # Pickle the arguments
        self._args = None  # Initially, the arguments are not loaded.

    def get_args(self):
        # This method is responsible for actually loading (unpickling) the arguments, but *only* when they are requested.
        if self._args is None:
            print(
                "Loading args..."
            )  # Simulate the loading/unpickling process. Crucially, this happens *after* environment setup.
            self._args = pickle.loads(self.pickled_args)  # Unpickle only when needed
        return self._args

    def __repr__(self):
        return f"<LazyArgs - Not Loaded>" if self._args is None else repr(self._args)


def task(
    _func: Optional[Callable[P_Args,R_Type]] = None,
    *,
    require_context: bool = False,
    function_name: Optional[str] = None,
) -> Union[Callable, Task]:
    def decorator(func: Callable[P_Args,R_Type]) -> Task[P_Args,R_Type]:
        resolved_name = function_name or func.__name__
        return Task[P_Args,R_Type](func, require_context=require_context, func_name=resolved_name)

    if _func is None:
        # Case 1: Called with arguments - @task(...)
        return decorator
    else:
        # Case 2: Called without arguments - @task
        return decorator(_func)
