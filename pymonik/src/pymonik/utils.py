import time
import grpc
import cloudpickle as pickle

from typing import List, Optional, Set, Union
from armonik.common import create_channel, Result, ResultStatus
from armonik.client import ArmoniKResults

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


def _poll_batch_for_results(
    results_client: ArmoniKResults,
    result_ids_in_batch: List[str],
    polling_interval_seconds: float,
) -> None:
    """
    Polls for the completion or abortion of a batch of results.
    """
    if not result_ids_in_batch:
        return

    not_found: Set[str] = set(result_ids_in_batch)

    while not_found:
        current_filter = None
        for r_id in not_found:
            filter_condition = (Result.result_id == r_id)
            if current_filter is None:
                current_filter = filter_condition
            else:
                current_filter = current_filter | filter_condition

        if current_filter is None: # Should not happen if not_found is populated
            break

        try:
            _total, fetched_results = results_client.list_results(
                result_filter=current_filter,
                page=0,
                page_size=len(not_found),
            )
            for res_summary in fetched_results: 
                if res_summary.result_id in not_found:
                    if res_summary.status == ResultStatus.COMPLETED:
                        not_found.remove(res_summary.result_id)
                    elif res_summary.status == ResultStatus.ABORTED:
                        raise RuntimeError(f"Result {res_summary.result_id} has been aborted.")

            if not not_found: # All results in this batch are completed
                break

        except grpc.RpcError:
            # Basic retry on RpcError.
            pass
        except RuntimeError: # Re-raise "Result ... has been aborted."
            raise
        except Exception as e:
            raise RuntimeError(f"An unexpected error occurred while polling for results batch: {e}")

        time.sleep(polling_interval_seconds)