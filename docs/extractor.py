#!/usr/bin/env python3

# Copyright (c) 2021, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Use a for-loop, to access
# multiple containers. The command-line argument
# is a release value, like "22.02".
# Between the release value and the container name,
# there is enough information to group the container
# information...
#
# {
#    "merlin-training": {
#       "22.02": {
#          "cuda": "11.6"
#       },
#       "22.01": {
#          "cuda": "11.5"
#       }
#    }
# }
#
# After all the data is gathered, it should be possible
# to construct a multi-release table.
# "merlin-training"
# |       | 22.02 | 22.01 |
# | ----- | ----- | ----- |
# | CUDA  |  11.6 |  11.5 |

import argparse
import contextlib
import json
import logging
import os
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml
from mergedeep import merge

import docker

level = logging.DEBUG if os.environ.get("DEBUG") else logging.INFO
logging.basicConfig(level=level)
logger = logging.getLogger("extractor")


@contextlib.contextmanager
def managed_container(img):
    client = docker.from_env()
    container = None
    try:
        # runtime="nvidia", # the runtime is not installed on GH runners.
        container = client.containers.run(
            img,
            command="bash",
            detach=True,
            ipc_mode="host",
            tty=True,
        )
        yield container
    except docker.errors.ImageNotFound as nf:
        yield nf
    except Exception as e:  # pylint: disable=broad-except
        yield e
    finally:
        if container:
            container.stop()
            container.remove()


def get_yymm() -> str:
    return date.today().strftime("%y.%m")


class SupportMatrixExtractor:

    contdata = {}
    data: defaultdict(dict)
    ERROR = "Not applicable"
    container: docker.models.containers.Container
    container_name: str
    release: str
    standard_snippets = ["dgx_system", "nvidia_driver", "gpu_model"]

    def __init__(self, name: str, release: str, datafile: str):
        self.container_name = name
        self.release = release
        self.contdata = {}
        self.data = {}
        self.data = defaultdict(dict)
        self.data[self.container_name][self.release] = self.contdata
        self.datafile = datafile

    def use_container(self, container: docker.models.containers.Container):
        self.container = container

    def get_from_envfile(self, path: str, lookup: str, key=None):
        if key is None:
            key = lookup
        self.contdata[key] = self.ERROR
        (err, output) = self.container.exec_run(
            "bash -c 'source {0}; echo ${{{1}}}'".format(path, lookup)
        )
        result = output.decode("utf-8")
        if err != 1 and not result.isspace():
            self.contdata[key] = result.replace('"', "").strip()
        else:
            logger.info("Failed to get env var '%s' from file '%s'", lookup, path)

    def get_from_env(self, lookup: str, key=None):
        if key is None:
            key = lookup
        self.contdata[key] = self.ERROR
        (err, output) = self.container.exec_run(
            "bash -c 'echo ${{{0}}}'".format(lookup)
        )
        result = output.decode("utf-8")
        if err != 1 and not result.isspace():
            self.contdata[key] = result.replace('"', "").strip()
        else:
            logger.info("Failed to get env var: '%s'", lookup)

    def get_from_pip(self, lookup: str, key=None):
        """Retrieves the version of a Python package from Pip. This function avoids importing
        the package which might not work on systems without a GPU.

        Returns `None` if the package isn't installed.
        """
        if key is None:
            key = lookup
        self.contdata[key] = self.ERROR
        (err, output) = self.container.exec_run(
            "python -m pip show '{}'".format(lookup)
        )
        if err != 0:
            logger.info("Failed to get package version from pip: %s", lookup)
            return
        versions = [
            line.split()[-1]
            for line in output.decode().split("\n")
            if line.startswith("Version:")
        ]
        if len(versions) == 1:
            self.contdata[key] = versions[0].strip()
        else:
            logger.info(
                "Failed to extract version from pip output: %s", output.decode()
            )

    def get_from_image(self, lookup: str, key=None):
        if key is None:
            key = lookup
        self.contdata[key] = self.ERROR
        attrs = self.container.image.attrs
        try:
            self.contdata[key] = attrs[lookup]
        except KeyError:
            logger.info("Failed to get attr from image: '%s'", lookup)
        if lookup == "Size":
            self.contdata[key] = "{} GB".format(
                round(attrs[lookup] / 1024 ** 3, 2)
            )  # noqa

    def get_from_cmd(self, cmd: str, key: str):
        self.contdata[key] = self.ERROR
        (err, output) = self.container.exec_run("bash -c '{}'".format(cmd))
        if err != 1:
            self.contdata[key] = output.decode("utf-8").strip()
            # Let the hacks begin...
            if key == "sm":
                smlist = output.decode("utf-8").split()
                self.contdata[key] = ", ".join(smlist)
        else:
            logger.info("Command '%s' failed: %s", cmd, output.decode())

    def insert_snippet(self, key: str, snip: str):
        self.contdata[key] = snip

    def to_json(self):
        return json.dumps(self.data, sort_keys=True)

    def from_json(self):
        if not os.path.exists(self.datafile):
            return

        with open(self.datafile) as f:
            _data = json.load(f)
            if self.container_name not in _data:
                _data[self.container_name] = {}
            if self.release not in _data[self.container_name]:
                _data[self.container_name][self.release] = {}
        self.data = merge({}, _data, self.data)
        self.data[self.container_name][self.release] = self.contdata

    def to_json_file(self):
        with open(self.datafile, "w") as f:
            json.dump(self.data, f, sort_keys=True, indent=2)


# pylint: disable=too-many-locals, too-many-statements
def main(args):
    # Images information
    ngc_base = "nvcr.io/nvidia/merlin/"
    containers = [
        "merlin-training",
        "merlin-tensorflow-training",
        "merlin-pytorch-training",
        "merlin-inference",
        "merlin-tensorflow-inference",
        "merlin-pytorch-inference",
    ]

    scriptdir = Path(__file__).parent

    jsonfile = scriptdir / "data.json"
    snippetsfile = scriptdir / "snippets.yaml"
    version = args.version

    if args.file:
        jsonfile = os.path.abspath(args.file)
    if args.snippets:
        snippetsfile = os.path.abspath(args.snippets)
    if not version:
        version = get_yymm()

    sniptext = {}
    with open(snippetsfile) as f:
        sniptext = yaml.safe_load(f)
        for k in SupportMatrixExtractor.standard_snippets:
            assert sniptext[k]

    # Iterate through the images and get information
    for cont in containers:
        img = ngc_base + cont + ":" + version
        logger.info("Extracting information from: %s", img)
        with managed_container(img) as container:
            if isinstance(container, Exception):
                logger.info("...image is not found.")
                continue

            logger.info("...container is running.")

            xtr = SupportMatrixExtractor(ngc_base + cont, version, jsonfile)
            xtr.use_container(container)
            xtr.from_json()

            for k in xtr.standard_snippets:
                xtr.insert_snippet(k, sniptext[k])
            xtr.insert_snippet("release", args.version)

            xtr.get_from_image("Size", "size")
            xtr.get_from_envfile("/etc/os-release", "PRETTY_NAME", "os")
            xtr.get_from_env("CUDA_VERSION", "cuda")
            xtr.get_from_pip("rmm")
            xtr.get_from_pip("cudf")
            xtr.get_from_env("CUDNN_VERSION", "cudnn")
            xtr.get_from_pip("nvtabular")
            xtr.get_from_pip("transformers4rec")
            xtr.get_from_pip("merlin.core")
            xtr.get_from_pip("merlin.systems")
            xtr.get_from_pip("merlin.models")
            xtr.get_from_pip("hugectr2onnx")
            xtr.get_from_pip("hugectr")
            xtr.get_from_pip("sparse_operation_kit")
            xtr.get_from_pip("tensorflow", "tf")
            xtr.get_from_pip("torch", "pytorch")
            xtr.get_from_env("CUBLAS_VERSION", "cublas")
            xtr.get_from_env("CUFFT_VERSION", "cufft")
            xtr.get_from_env("CURAND_VERSION", "curand")
            xtr.get_from_env("CUSOLVER_VERSION", "cusolver")
            xtr.get_from_env("CUSPARSE_VERSION", "cusparse")
            xtr.get_from_env("CUTENSOR_VERSION", "cutensor")
            xtr.get_from_env("NVIDIA_TENSORFLOW_VERSION", "nvidia_tensorflow")
            xtr.get_from_env("NVIDIA_PYTORCH_VERSION", "nvidia_pytorch")
            xtr.get_from_env("OPENMPI_VERSION", "openmpi")
            xtr.get_from_env("TRT_VERSION", "tensorrt")
            xtr.get_from_env("TRTOSS_VERSION", "base_container")
            # xtr.get_from_cmd("cuobjdump /usr/local/hugectr/lib/libhuge_ctr_shared.so
            # | grep arch | sed -e \'s/.*sm_//\' | sed -e \'H;${x;s/\\n/, /g;s/^, //;p};d\'", "sm")
            # flake8: noqa
            xtr.get_from_cmd(
                "if [ ! -f /usr/local/hugectr/lib/libhuge_ctr_shared.so ]; then exit 1; fi; cuobjdump /usr/local/hugectr/lib/libhuge_ctr_shared.so | grep arch | sed -e 's/.*sm_//'",
                "sm",
            )
            xtr.get_from_cmd("cat /opt/tritonserver/TRITON_VERSION", "triton")
            xtr.get_from_cmd(
                'python -c "import sys;print(sys.version_info[0]);"', "python_major"
            )

            # Some hacks for the base container image
            if cont == "merlin-training":
                xtr.insert_snippet("base_container", "Not applicable")
            elif cont == "merlin-tensorflow-training":
                tf2_img = xtr.contdata["nvidia_tensorflow"]
                py_maj = xtr.contdata["python_major"]
                xtr.insert_snippet(
                    "base_container",
                    "nvcr.io/nvidia/tensorflow:{}-py{}".format(tf2_img, py_maj),
                )
            elif cont == "merlin-pytorch-training":
                pt_img = xtr.contdata["nvidia_pytorch"]
                py_maj = xtr.contdata["python_major"]
                xtr.insert_snippet(
                    "base_container",
                    "nvcr.io/nvidia/pytorch:{}-py{}".format(pt_img, py_maj),
                )
            else:
                trtoss = xtr.contdata["base_container"]
                xtr.insert_snippet("base_container", "Triton version {}".format(trtoss))

            xtr.to_json_file()

            logger.info(xtr.contdata)


def parse_args():
    """
    Use the versions script setting Merlin version to explore
    python extractor.py -v 22.03
    """
    parser = argparse.ArgumentParser(description=("Container Extraction Tool"))
    # Containers version
    parser.add_argument(
        "-v",
        "--version",
        type=str,
        help="Version in YY.MM format",
    )

    parser.add_argument(
        "-f",
        "--file",
        type=str,
        help="JSON data file",
    )

    parser.add_argument(
        "-s",
        "--snippets",
        type=str,
        help="YAML snippets file",
    )

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    main(parse_args())
