import cloudpickle as pickle
from typing import Generic, TypeVar, get_args, List

T = TypeVar("T")


# TODO: Generics for better typing ... ResultHandle[str] for example..
class ResultHandle(Generic[T]):
    """A handle to a future result from an ArmoniK task."""

    def __init__(self, result_id: str, session_id: str, pymonik_instance: "Pymonik"):
        self.result_id = result_id
        self.session_id: str = session_id
        self._pymonik = pymonik_instance

    def wait(self) -> "ResultHandle[T]":
        """Wait for the result to be available."""
        if self._pymonik.is_worker():
            raise RuntimeError(
                "Cannot wait for result in worker context. Use the client context instead."
            )
        try:
            self._pymonik._events_client.wait_for_result_availability(
                self.result_id, self.session_id
            )
            return self
        except Exception as e:
            print(f"Error waiting for result {self.result_id}: {e}")
            raise

    def get(self) -> T:
        """Get the result value."""
        result_data = self._pymonik._results_client.download_result_data(
            self.result_id, self.session_id
        )
        return pickle.loads(result_data)

    def __repr__(self):
        type_str = "T"  # Default to the TypeVar name if not specialized

        try:
            # __orig_class__ is set if the instance is created from a
            # specialized generic type, e.g. ResultHandle[str]
            if hasattr(self, "__orig_class__"):
                type_args = get_args(self.__orig_class__)
                if type_args:
                    actual_type_arg = type_args[0]
                    if isinstance(actual_type_arg, TypeVar):
                        type_str = actual_type_arg.__name__  # e.g., "T"
                    else:
                        # For concrete types like <class 'str'> or typing.List[int]
                        type_str = str(actual_type_arg)
                        # Make it prettier
                        if type_str.startswith("typing."):
                            type_str = type_str[len("typing.") :]
                        if type_str.startswith("<class '"):  # e.g. <class 'int'> -> int
                            type_str = type_str[len("<class '") : -2]
            elif (
                hasattr(self.__class__, "__parameters__")
                and self.__class__.__parameters__
            ):
                # Fallback if __orig_class__ isn't available or not specific,
                # get the name of the TypeVar from the class definition itself.
                # self.__class__.__parameters__ is a tuple of TypeVars (e.g., (~T,))
                type_str = self.__class__.__parameters__[0].__name__

        except Exception:
            # In case of any introspection error, fallback gracefully
            type_str = "Unknown"
        return f"<ResultHandle(id={self.result_id}, session={self.session_id}, type={type_str})>"


# TODO: implement _results_as_completed for retrieving results as they're completed
# nvm maybe this is better, it'd be weird to fetch things when you iterate, implicit behavior bad..
class MultiResultHandle:
    """A handle to multiple future results from ArmoniK tasks."""

    def __init__(self, result_handles: List[ResultHandle]):
        self.result_handles = result_handles
        if result_handles:
            self._pymonik = result_handles[0]._pymonik
            self.session_id = result_handles[0].session_id
        else:
            self._pymonik = None
            self.session_id = None

    def wait(self):
        """Wait for all results to be available."""
        if not self.result_handles:
            return self

        result_ids = [handle.result_id for handle in self.result_handles]
        try:
            self._pymonik._events_client.wait_for_result_availability(
                result_ids, self.session_id
            )
            return self
        except Exception as e:
            print(f"Error waiting for results: {e}")
            raise

    def get(self):
        """Get all result values."""
        # TODO: maybe should cache the get
        return [handle.get() for handle in self.result_handles]

    def __iter__(self):
        return iter(self.result_handles)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return MultiResultHandle(self.result_handles[index])
        elif isinstance(index, int):
            return self.result_handles[index]
        else:
            raise TypeError("Index must be an integer or a slice.")

    def __len__(self):
        return len(self.result_handles)

    def __repr__(self):
        return f"<MultiResultHandle(results={self.result_handles})>"
