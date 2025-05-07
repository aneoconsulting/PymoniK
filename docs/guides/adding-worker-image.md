You can add a new worker image to your ArmoniK cluster by creating a partition, inside your control plane

```tf
  # Partition for the PymoniK worker
  pymonik = {
    # number of replicas for each deployment of compute plane
    replicas = 0 #(1)!
    # ArmoniK polling agent
    polling_agent = {
      limits = {
        cpu    = "2000m"
        memory = "2048Mi"
      }
      requests = {
        cpu    = "50m"
        memory = "50Mi"
      }
    }
    # ArmoniK workers
    worker = [
      {
        image = "harmonic_snake"
        tag   = "python-YOUR_PYTHON_VERSION-PYMONIK_VERSION_TO_USE" #(2)!
        limits = {
          cpu    = "1000m"
          memory = "1024Mi"
        }
        requests = {
          cpu    = "50m"
          memory = "50Mi"
        }
      }
    ]
    hpa = {
      type              = "prometheus"
      polling_interval  = 15
      cooldown_period   = 300
      min_replica_count = 0
      max_replica_count = 5
      behavior = {
        restore_to_original_replica_count = true
        stabilization_window_seconds      = 300
        type                              = "Percent"
        value                             = 100
        period_seconds                    = 15
      }
      triggers = [
        {
          type      = "prometheus"
          threshold = 2
        },
      ]
    }
  },
```

1. By default this partition will start with no workers and scale up as needed, you can change this behavior for faster cold starts
2. Don't forget to set the version of python that you're using here, it **must match** the version of python that you're using for your client. The second part of the tag is for the PymoniK package version to use. 

For the list of available docker images tags, please refer to [our repository](https://hub.docker.com/r/dockerhubaneo/harmonic_snake)
