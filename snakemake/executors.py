
import os
import time
import textwrap
import stat
import shutil
import random
import string
import threading
import concurrent.futures
from functools import partial
from itertools import chain

from snakemake.jobs import Job
from snakemake.shell import shell
from snakemake.logging import logger
from snakemake.stats import Stats
from snakemake.utils import format
from snakemake.exceptions import print_exception, get_exception_origin
from snakemake.exceptions import format_error, RuleException
from snakemake.exceptions import ClusterJobException, ProtectedOutputException


class AbstractExecutor:

    def __init__(self, workflow, dag,
        printreason=False, quiet=False,
        printshellcmds=False, printthreads=True, output_wait=3):
        self.workflow = workflow
        self.dag = dag
        self.quiet = quiet
        self.printreason = printreason
        self.printshellcmds = printshellcmds
        self.printthreads = printthreads
        self.output_wait = output_wait

    def run(self, job, callback=None, error_callback=None):
        self._run(job)
        callback(job)

    def shutdown(self):
        pass

    def _run(self, job):
        self.printjob(job)

    def printjob(self, job):

        def format_files(job, io, ruleio, dynamicio):
            for f, f_ in zip(io, ruleio):
                if f in dynamicio:
                    yield "{} (dynamic)".format(f_)
                else:
                    yield f

        def format_ruleitem(name, value):
            return "" if not value else "\t{}: {}".format(name, value)

        desc = list()
        if not self.quiet:
            if job.message:
                desc.append(job.message)
            else:
                desc.append("rule {}:".format(job.rule.name))
                for name, value in (
                    ("input", ", ".join(format_files(
                        job, job.input, job.rule.input, job.dynamic_input))),
                    ("output", ", ".join(format_files(
                        job, job.output, job.rule.output,
                        job.dynamic_output))),
                    ("log", job.log),
                    ("reason",
                        self.dag.reason(job) if self.printreason else None)):
                    if value:
                        desc.append(format_ruleitem(name, value))
                if job.priority > 1:
                    desc.append(format_ruleitem(
                        "priority", "highest"
                        if job.priority == Job.HIGHEST_PRIORITY
                        else job.priority))
                if self.printthreads and job.threads > 1:
                    desc.append(format_ruleitem("threads", job.threads))
        if self.printshellcmds and job.shellcmd:
            desc.append(job.shellcmd)
        if desc:
            logger.info("\n".join(desc))
            if job.dynamic_output:
                logger.warning("Subsequent jobs will be added dynamically "
                    "depending on the output of this rule")

    def finish_job(self, job):
        self.dag.check_output(job, wait=self.output_wait)
        self.dag.handle_protected(job)
        self.dag.handle_temp(job)


class DryrunExecutor(AbstractExecutor):
    pass


class RealExecutor(AbstractExecutor):

    def __init__(
        self, workflow, dag,
        printreason=False, quiet=False, printshellcmds=False, output_wait=3):
        super().__init__(
            workflow, dag, printreason=printreason,
            quiet=quiet, printshellcmds=printshellcmds,
            output_wait=output_wait)
        self.stats = Stats()

    def _run(self, job, callback=None, error_callback=None):
        super()._run(job)
        self.stats.report_job_start(job)
        try:
            self.workflow.persistence.started(job)
        except IOError as e:
            logger.warning("Failed to set marker file for job started ({}). "
                "Snakemake will work, but cannot ensure that output files "
                "are complete in case of a kill signal or power loss. "
                "Please ensure write permissions for the "
                "directory {}".format(
                    e, self.workflow.persistence.path))

    def finish_job(self, job):
        super().finish_job(job)
        self.stats.report_job_end(job)
        try:
            self.workflow.persistence.finished(job)
        except IOError as e:
            logger.warning("Failed to remove marker file for job started "
                "({}). Please ensure write permissions for the "
                "directory {}".format(
                    e, self.workflow.persistence.path))


class TouchExecutor(RealExecutor):

    def run(self, job, callback=None, error_callback=None):
        super()._run(job)
        try:
            for f in job.expanded_output:
                f.touch()
            time.sleep(0.1)
            self.finish_job(job)
            callback(job)
        except OSError as ex:
            print_exception(ex, self.workflow.linemaps)
            error_callback()


class CPUExecutor(RealExecutor):

    def __init__(
        self, workflow, dag, cores, printreason=False, quiet=False,
        printshellcmds=False, threads=False, output_wait=3):
        super().__init__(
            workflow, dag, printreason=printreason, quiet=quiet,
            printshellcmds=printshellcmds, output_wait=output_wait)
        self.pool = (concurrent.futures.ThreadPoolExecutor(max_workers=cores)
            if threads
            else concurrent.futures.ProcessPoolExecutor(max_workers=cores))

    def run(self, job, callback=None, error_callback=None):
        super()._run(job)

        job.prepare()

        future = self.pool.submit(
            run_wrapper, job.rule.run_func, job.input, job.output, job.params,
            job.wildcards, job.threads, job.log, self.workflow.linemaps)
        future.add_done_callback(partial(
            self._callback, job, callback, error_callback))

    def shutdown(self):
        self.pool.shutdown()

    def _callback(self, job, callback, error_callback, future):
        try:
            ex = future.exception()
            if ex:
                raise ex
            self.finish_job(job)
            callback(job)
        except (Exception, BaseException) as ex:
            print_exception(ex, self.workflow.linemaps)
            job.cleanup()
            error_callback()


class ClusterExecutor(RealExecutor):

    jobscript = textwrap.dedent("""
        #!/bin/sh
        #rule: {job}
        #input: {job.input}
        #output: {job.output}
        {self.workflow.snakemakepath} --snakefile {self.workflow.snakefile} \
        --force -j{self.cores} \
        --directory {workdir} --nocolor --quiet --nolock {job.output} \
        && touch "{jobfinished}" || touch "{jobfailed}"
        exit 0
        """)

    def __init__(
        self, workflow, dag, cores, submitcmd="qsub",
        printreason=False, quiet=False, printshellcmds=False, output_wait=3):
        super().__init__(
            workflow, dag, printreason=printreason, quiet=quiet,
            printshellcmds=printshellcmds, output_wait=output_wait)
        if workflow.snakemakepath is None:
            raise ValueError(
            "Cluster executor needs to know the path "
            "to the snakemake binary.")
        self.submitcmd = submitcmd
        self.startedjobs = 0
        self._tmpdir = None
        self.cores = cores if cores else ""

    def shutdown(self):
        shutil.rmtree(self.tmpdir)

    def run(self, job, callback=None, error_callback=None):
        super()._run(job)
        workdir = os.getcwd()
        jobid = self.startedjobs

        jobscript = os.path.join(self.tmpdir, "{}.sh".format(jobid))
        jobfinished = os.path.join(self.tmpdir, "{}.jobfinished".format(jobid))
        jobfailed = os.path.join(self.tmpdir, "{}.jobfailed".format(jobid))
        with open(jobscript, "w") as f:
            print(format(self.jobscript), file=f)
        os.chmod(jobscript, os.stat(jobscript).st_mode | stat.S_IXUSR)

        submitcmd = job.format_wildcards(self.submitcmd)
        shell('{submitcmd} "{jobscript}"')

        self.startedjobs += 1
        threading.Thread(
            target=self._wait_for_job,
            args=(job, callback, error_callback,
                jobscript, jobfinished, jobfailed)).start()

    def _wait_for_job(
        self, job, callback, error_callback,
        jobscript, jobfinished, jobfailed):
        while True:
            if os.path.exists(jobfinished):
                os.remove(jobfinished)
                os.remove(jobscript)
                self.finish_job(job)
                callback(job)
                return
            if os.path.exists(jobfailed):
                os.remove(jobfailed)
                os.remove(jobscript)
                print_exception(
                    ClusterJobException(job), self.workflow.linemaps)
                error_callback()
                return
            time.sleep(1)

    @property
    def tmpdir(self):
        if self._tmpdir is None:
            while True:
                self._tmpdir = ".snakemake.tmp." + "".join(random.sample(
                    string.ascii_uppercase + string.digits, 6))
                if not os.path.exists(self._tmpdir):
                    os.mkdir(self._tmpdir)
                    break
        return self._tmpdir


def run_wrapper(run, input, output, params, wildcards, threads, log, linemaps):
    """
    Wrapper around the run method that handles directory creation and
    output file deletion on error.

    Arguments
    run       -- the run method
    input     -- list of input files
    output    -- list of output files
    wildcards -- so far processed wildcards
    threads   -- usable threads
    log       -- path to log file
    """
    try:
        # execute the actual run method.
        run(input, output, params, wildcards, threads, log)
        # finish all spawned shells.
        shell.join_all()
    except (Exception, BaseException) as ex:
        # this ensures that exception can be re-raised in the parent thread
        lineno, file = get_exception_origin(ex, linemaps)
        raise RuleException(format_error(
            ex, lineno, linemaps=linemaps, snakefile=file,
            show_traceback=True))