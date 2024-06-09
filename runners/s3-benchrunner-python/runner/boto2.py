from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

import  threading, datetime, time

from runner import BenchmarkConfig, BenchmarkRunner

import pandas as pd
dic = {}

completed_connections = 0



class PerObjStat(object):

    def __init__(self, obj_name, obj_size):
        self._obj_name =obj_name
        self._obj_size = obj_size
        self._seen_so_far = 0
        self._lock = threading.Lock()
        self.latency = 0
        self.start = int(time.time_ns() / 1000000)
        self.sec_first_byte = 0


    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount
            time_now = int(time.time_ns() / 1000000)
            if self.sec_first_byte == 0:
                self.sec_first_byte = time_now - self.start
                            
            if self._seen_so_far >= self._obj_size:
                self.latency = time_now - self.start
                global dic
                dic[self._obj_name] = self.latency
                print(self._obj_name, self.sec_first_byte, self.latency)

class Boto3BenchmarkRunner(BenchmarkRunner):
    """Benchmark runner using boto3.client('s3')"""

    def __init__(self, config: BenchmarkConfig, use_crt: bool):
        super().__init__(config)

        # Currently (Jan 2024) users can't explicitly choose CRT in boto3.
        # Boto3 only uses CRT if it's installed, and a series of checks all pass.
        # Therefore, do some monkey-patching to get the results we want.
        try:
            import awscrt  # type: ignore
            import awscrt.s3  # type: ignore

            # when CRT is installed from source, its version is "1.0.0.dev0"
            # but boto3 expects it to be like "<int>.<int>.<int>"
            # patch function that boto3 calls to see if it should use CRT
            def patched_is_optimized_for_system():
                return use_crt
            awscrt.s3.is_optimized_for_system = patched_is_optimized_for_system

            # patch function that boto3 calls to get this machine's target throughput
            def patched_recommended_throughput():
                return config.target_throughput_Gbps
            awscrt.s3.get_recommended_throughput_target_gbps = patched_recommended_throughput

        except ModuleNotFoundError:
            # awscrt only needs to be installed if we're benchmarking CRT
            if use_crt:
                raise

        import boto3  # type: ignore
        import boto3.s3.transfer  # type: ignore

        self.use_crt = use_crt
        if (use_crt):
            self._verbose('--- boto3-crt ---')
        else:
            self._verbose('--- boto3-classic ---')

        self._s3_client = boto3.client('s3', region_name=config.region)

        # Set up boto3 TransferConfig
        # NOTE 1: Only SOME settings are used by both CRT and pure-python impl.
        # NOTE 2: Don't set anything that's not publicly documented here:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/customizations/s3.html
        transfer_kwargs = {
            'preferred_transfer_client': 'auto' if use_crt else 'classic',
        }
        if not use_crt:
            # I've tried tweaking settings to get performance up,
            # but I'm not seeing a difference on benchmarks of single large files...
            # transfer_kwargs['max_concurrency'] = os.cpu_count()
            # transfer_kwargs['max_io_queue'] = 1000
            pass

#        transfer_kwargs['max_concurrency'] = 5
        self._transfer_config = boto3.s3.transfer.TransferConfig(
            **transfer_kwargs)

        # report settings used by CRT and pure-python impl
        self._verbose_config('multipart_chunksize')
        self._verbose_config('multipart_threshold')
        if not use_crt:
            # report settings that only the pure-python impl uses, including
            # undocumented stuff on the s3transfer.manager.TransferConfig base class
            self._verbose_config('max_bandwidth')
            self._verbose_config('max_request_concurrency')
            self._verbose_config('max_submission_concurrency')
            self._verbose_config('max_request_queue_size')
            self._verbose_config('max_submission_queue_size')
            self._verbose_config('max_io_queue_size')
            self._verbose_config('io_chunksize')
            self._verbose_config('num_download_attempts')
            self._verbose_config('max_in_memory_upload_chunks')
            self._verbose_config('max_in_memory_download_chunks')

    def _verbose_config(self, attr_name):
        self._verbose(
            f'  {attr_name}: {getattr(self._transfer_config, attr_name)}')


    def _make_request(self, task_i: int):
        task = self.config.tasks[task_i]
        stats = PerObjStat(task.key, task.size)
        
       # start_time = time.time()
       # global dic
       # dic[task.key]= start_time
        #print(task.key, start_time)

        call_name = None
        call_kwargs = {
            'Bucket': self.config.bucket,
            'Key': task.key,
            'ExtraArgs': {},
            'Config': self._transfer_config,
            'Callback' : stats,
        }

        if task.action == 'upload':
            if self.config.files_on_disk:
                call_name = 'upload_file'
                call_kwargs['Filename'] = task.key
            else:
                call_name = 'upload_fileobj'
                call_kwargs['Fileobj'] = self._new_iostream_to_upload_from_ram(
                    task.size)

            # NOTE: botocore will add a checksum for uploads, even if we don't
            # tell it to (falls back to Content-MD5)
            if self.config.checksum:
                call_kwargs['ExtraArgs']['ChecksumAlgorithm'] = self.config.checksum

        elif task.action == 'download':
            if self.config.files_on_disk:
                call_name = 'download_file'
                call_kwargs['Filename'] = task.key
            else:
                call_name = 'download_fileobj'
                call_kwargs['Fileobj'] = Boto3DownloadFileObj()

            # boto3 doesn't validate download checksums unless you tell it to
            if self.config.checksum:
                call_kwargs['ExtraArgs']['ChecksumMode'] = 'ENABLED'

        else:
            raise RuntimeError(f'Unknown action: {task.action}')

        self._verbose(
            f"{call_name} {call_kwargs['Key']} ExtraArgs={call_kwargs['ExtraArgs']}")

        method = getattr(self._s3_client, call_name)
        method(**call_kwargs)

    def req_cb(self, stats):
        global dic
        dic[stats._obj_name] = stats.latency
        


    def run(self):
        # boto3 is a synchronous API, but we can run requests in parallel
        # so do that in a threadpool
        n_workers = 2
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            # submit tasks to threadpool
            task_futures_to_idx = {}
            for task_i in range(len(self.config.tasks)):
                task_future = executor.submit(self._make_request, task_i)
            #    task_future.add_done_callback(self.req_cb(self.config.tasks[task_i].stats))
                task_futures_to_idx[task_future] = task_i

            # wait until all tasks are done
            for task_future in as_completed(task_futures_to_idx):
                try:
                    task_future.result()
                except Exception as e:
                    task_i = task_futures_to_idx[task_future]
                    print(f'Failed on task {task_i+1}/{len(self.config.tasks)}: {self.config.tasks[task_i]}',
                          file=sys.stderr)

                    # cancel remaining tasks
                    executor.shutdown(cancel_futures=True)

                    raise e

        df = pd.DataFrame(dic.items(), columns = ['key','lat'])
        #print(df)
        df['lat'] = df['lat']
        print("min latency", df['lat'].min())
        print("ave latency", df['lat'].mean())
        print("max latency", df['lat'].max())
        now = datetime.datetime.now()
        fname = "/root/latency_results_" + str(now.time())
        df.to_csv(fname, sep=',')
        fname = "/root/latency_results_" + str(now.time()) +'_summary'
        fd = open(fname, "w")
        #fd.write("File: %s , thread: %s\n" % (task[i].key, max_concurrency))
        fd.write("min latency: %s\n" % (df['lat'].min()) )
        fd.write("ave latency: %s\n" % (df['lat'].mean()) )
        fd.write("max latency: %s\n" % (df['lat'].max()) )
        fd.close()



class Boto3DownloadFileObj:
    """File-like object that Boto3Benchmark downloads into when files_on_disk == False"""

    def write(self, b):
        # lol do nothing
        pass