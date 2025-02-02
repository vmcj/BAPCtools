import os
import sys

import program
import config
import interactive
import parallel
import validate
from typing import Type

from util import *
from colorama import Fore, Style


class Run:
    def __init__(self, problem, submission, testcase):
        self.problem = problem
        self.submission = submission
        self.testcase = testcase
        self.name = self.testcase.name
        self.result = None

        tmp_path = (
            self.problem.tmpdir / 'runs' / self.submission.short_path / self.testcase.short_path
        )
        self.out_path = tmp_path.with_suffix('.out')
        self.feedbackdir = tmp_path.with_suffix('.feedbackdir')
        self.feedbackdir.mkdir(exist_ok=True, parents=True)
        # Clean all files in feedbackdir.
        for f in self.feedbackdir.iterdir():
            if f.is_file():
                f.unlink()
            else:
                shutil.rmtree(f)

    # Return an ExecResult object amended with verdict.
    def run(self, *, interaction=None, submission_args=None):
        if self.problem.interactive:
            result = interactive.run_interactive_testcase(
                self, interaction=interaction, submission_args=submission_args
            )
        else:
            result = self.submission.run(self.testcase.in_path, self.out_path)
            if result.duration > self.problem.settings.timelimit:
                result.verdict = 'TIME_LIMIT_EXCEEDED'
                if result.timeout_expired:
                    result.print_verdict_ = 'TLE (aborted)'
            elif result.status == ExecStatus.ERROR:
                result.verdict = 'RUN_TIME_ERROR'
                if config.args.error:
                    result.err = 'Exited with code ' + str(result.returncode) + ':\n' + result.err
                else:
                    result.err = 'Exited with code ' + str(result.returncode)
            else:
                # Overwrite the result with validator returncode and stdout/stderr, but keep the original duration.
                duration = result.duration
                result = self._validate_output()
                if result is None:
                    error(f'No output validators found for testcase {self.testcase.name}')
                    result = ExecResult(None, ExecStatus.REJECTED, 0, False, None, None)
                    result.verdict = 'VALIDATOR_CRASH'
                else:
                    result.duration = duration

                    if result.status:
                        result.verdict = 'ACCEPTED'
                    elif result.status == ExecStatus.REJECTED:
                        result.verdict = 'WRONG_ANSWER'
                    else:
                        config.n_error += 1
                        result.verdict = 'VALIDATOR_CRASH'

            # Delete .out files larger than 1MB.
            if (
                not config.args.error
                and self.out_path.is_file()
                and self.out_path.stat().st_size > 1_000_000_000
            ):
                self.out_path.unlink()

        self.result = result
        return result

    def _validate_output(self):
        output_validators = self.problem.validators(validate.OutputValidator)
        if output_validators is False:
            return None
        assert len(output_validators) == 1
        validator = output_validators[0]

        flags = self.testcase.testdata_yaml_validator_flags(validator)

        ret = validator.run(self.testcase, self, args=flags)

        judgemessage = self.feedbackdir / 'judgemessage.txt'
        judgeerror = self.feedbackdir / 'judgeerror.txt'
        if ret.err is None:
            ret.err = ''
        if judgemessage.is_file():
            ret.err += judgemessage.read_text()
            judgemessage.unlink()
        if judgeerror.is_file():
            # Remove any std output because it will usually only contain the
            ret.err = judgeerror.read_text()
            judgeerror.unlink()
        if ret.err:
            header = validator.name + ': ' if len(output_validators) > 1 else ''
            ret.err = header + ret.err

        return ret


class Submission(program.Program):
    subdir = 'submissions'

    def __init__(self, problem, path, skip_double_build_warning=False):
        super().__init__(problem, path, skip_double_build_warning=skip_double_build_warning)

        self.verdict = None
        self.duration = None

        # The first element will match the directory the file is in, if possible.
        self.expected_verdicts = self._get_expected_verdicts()

        # NOTE: Judging of interactive problems on systems without `os.wait4` is
        # suboptimal because we cannot determine which of the submission and
        # interactor exits first. Thus, we don't distinguish the different non-AC
        # verdicts.
        if self.problem.interactive and (is_windows() or is_bsd()):
            wrong_verdicts = ['WRONG_ANSWER', 'TIME_LIMIT_EXCEEDED', 'RUN_TIME_ERROR']
            for wrong_verdict in wrong_verdicts:
                if wrong_verdict in self.expected_verdicts:
                    self.expected_verdicts += wrong_verdicts
                    break

    def _get_expected_verdicts(self):
        verdicts = []

        # Look for '@EXPECTED_RESULTS@: ' in all source files. This should be followed by a comma separated list of the following:
        # - ACCEPTED / CORRECT
        # - WRONG_ANSWER / WRONG-ANSWER / NO-OUTPUT
        # - TIME_LIMIT_EXCEEDED / TIMELIMIT
        # - RUN_TIME_ERROR / RUN-ERROR
        domjudge_verdict_map = {
            'CORRECT': 'ACCEPTED',
            'WRONG-ANSWER': 'WRONG_ANSWER',
            'TIMELIMIT': 'TIME_LIMIT_EXCEEDED',
            'RUN-ERROR': 'RUN_TIME_ERROR',
            'NO-OUTPUT': 'WRONG_ANSWER',
            'CHECK-MANUALLY': None,
            'COMPILER-ERROR': None,
        }
        # Matching is case insensitive and all source files are checked.
        key = '@EXPECTED_RESULTS@: '
        if self.path.is_file():
            files = [self.path]
        elif self.path.is_dir():
            files = self.path.glob('**/*')
        else:
            files = []
        for f in files:
            if not f.is_file():
                continue
            try:
                text = f.read_text().upper()
                beginpos = text.index(key) + len(key)
                endpos = text.find('\n', beginpos)
                arguments = map(str.strip, text[beginpos:endpos].split(','))
                for arg in arguments:
                    if arg in domjudge_verdict_map:
                        arg = domjudge_verdict_map[arg]
                        if arg is None:
                            continue
                    if arg not in config.VERDICTS:
                        error(
                            f'@EXPECTED_RESULTS@: `{arg}` for submission {self.short_path} is not valid'
                        )
                        continue
                    verdicts.append(arg)
                break
            except (UnicodeDecodeError, ValueError):
                # Skip binary files.
                # Skip files where the key does not occur.
                pass

        if len(self.path.parts) >= 3 and self.path.parts[-3] == 'submissions':
            # Submissions in any of config.VERDICTS should not have `@EXPECTED_RESULTS@: `, and vice versa.
            # See https://github.com/DOMjudge/domjudge/issues/1861
            subdir = self.short_path.parts[0].upper()
            if subdir in config.VERDICTS:
                if len(verdicts) != 0:
                    warn(f'@EXPECTED_RESULTS@ in submission {self.short_path} is ignored.')
                verdicts = [subdir]
            else:
                if len(verdicts) == 0:
                    error(
                        f'Submission {self.short_path} must have @EXPECTED_RESULTS@. Defaulting to ACCEPTED.'
                    )

        if len(verdicts) == 0:
            verdicts = ['ACCEPTED']
        return verdicts

    # Run submission on in_path, writing stdout to out_path or stdout if out_path is None.
    # args is used by SubmissionInvocation to pass on additional arguments.
    # Returns ExecResult
    # The `default_timeout` argument is used when a submission is run as a solution when
    # generating testcases.
    def run(self, in_path, out_path, crop=True, args=[], cwd=None, default_timeout=False):
        assert self.run_command is not None
        # Just for safety reasons, change the cwd.
        if cwd is None:
            cwd = self.tmpdir
        with in_path.open('rb') as inf:
            out_file = out_path.open('wb') if out_path else None

            # Print stderr to terminal is stdout is None, otherwise return its value.
            result = exec_command(
                self.run_command + args,
                crop=crop,
                stdin=inf,
                stdout=out_file,
                stderr=None if out_file is None else True,
                timeout=True if default_timeout else self.problem.settings.timeout,
                cwd=cwd,
            )
            if out_file:
                out_file.close()
            return result

    # Run this submission on all testcases for the current problem.
    # Returns (OK verdict, printed newline)
    def run_all_testcases(
        self, max_submission_name_len=None, table_dict=None, *, needs_leading_newline
    ):
        runs = [Run(self.problem, self, testcase) for testcase in self.problem.testcases()]
        max_item_len = max(len(run.name) for run in runs) + max_submission_name_len - len(self.name)

        bar = ProgressBar(
            'Running ' + self.name,
            count=len(runs),
            max_len=max_item_len,
            needs_leading_newline=needs_leading_newline,
        )

        max_duration = -1

        verdict = (-100, 'ACCEPTED', 'ACCEPTED', 0)  # priority, verdict, print_verdict, duration
        verdict_run = None

        def process_run(run, p):
            nonlocal max_duration, verdict, verdict_run

            localbar = bar.start(run)
            result = run.run()

            if result.verdict == 'ACCEPTED' and not self.problem.interactive:
                validate.sanity_check(run.out_path, localbar, strict_whitespace=False)

            new_verdict = (
                config.PRIORITY[result.verdict],
                result.verdict,
                result.print_verdict(),
                result.duration,
            )
            if new_verdict > verdict:
                verdict = new_verdict
                verdict_run = run
            max_duration = max(max_duration, result.duration)

            if table_dict is not None:
                table_dict[run.name] = result.verdict == 'ACCEPTED'

            got_expected = result.verdict in ['ACCEPTED'] + self.expected_verdicts

            # Print stderr whenever something is printed
            if result.out and result.err:
                output_type = 'PROGRAM STDERR' if self.problem.interactive else 'STDOUT'
                data = (
                    f'STDERR:'
                    + localbar._format_data(result.err)
                    + f'\n{output_type}:'
                    + localbar._format_data(result.out)
                    + '\n'
                )
            else:
                data = ''
                if result.err:
                    data = crop_output(result.err)
                if result.out:
                    data = crop_output(result.out)

            # Add data from feedbackdir.
            for f in run.feedbackdir.iterdir():
                if not f.is_file():
                    localbar.warn(f'Validator wrote to {f} but it\'s not a file.')
                    continue
                try:
                    t = f.read_text()
                except UnicodeDecodeError:
                    localbar.warn(
                        f'Validator wrote to {f} but it cannot be parsed as unicode text.'
                    )
                    continue
                f.unlink()
                if not t:
                    continue
                if len(data) > 0 and data[-1] != '\n':
                    data += '\n'
                data += f'{f.name}:' + localbar._format_data(t) + '\n'

            localbar.done(got_expected, f'{result.duration:6.3f}s {result.print_verdict()}', data)

            # Lazy judging: stop on the first error when:
            # - not in verbose mode
            if config.args.verbose or config.args.table:
                return
            # - the result has max priority
            if result.verdict not in config.MAX_PRIORITY_VERDICT:
                return
            # - for TLE, the run was aborted because the global timeout expired
            if result.verdict == 'TIME_LIMIT_EXCEEDED' and not result.timeout_expired:
                return

            bar.count = None
            p.abort()

        p = parallel.new_queue(lambda run: process_run(run, p), pin=True)
        for run in runs:
            p.put(run)
        p.done()

        self.verdict = verdict[1]
        self.print_verdict = verdict[2]
        self.duration = max_duration

        # Use a bold summary line if things were printed before.
        if bar.logged:
            color = (
                Style.BRIGHT + Fore.GREEN
                if self.verdict in self.expected_verdicts
                else Style.BRIGHT + Fore.RED
            )
            boldcolor = Style.BRIGHT
        else:
            color = Fore.GREEN if self.verdict in self.expected_verdicts else Fore.RED
            boldcolor = ''

        printed_newline = bar.finalize(
            message=f'{max_duration:6.3f}s {color}{self.print_verdict:<20}{Style.RESET_ALL} @ {verdict_run.testcase.name}'
        )

        return (self.verdict in self.expected_verdicts, printed_newline)

    def test(self):
        print(ProgressBar.action('Running', str(self.name)), file=sys.stderr)

        testcases = self.problem.testcases(needans=False)

        if self.problem.interactive:
            output_validators = self.problem.validators('output')
            if output_validators is False:
                return

        for testcase in testcases:
            header = ProgressBar.action('Running ' + str(self.name), testcase.name)
            print(header, file=sys.stderr)

            if not self.problem.interactive:
                assert self.run_command is not None
                with testcase.in_path.open('rb') as inf:
                    result = exec_command(
                        self.run_command,
                        crop=False,
                        stdin=inf,
                        stdout=None,
                        stderr=None,
                        timeout=self.problem.settings.timeout,
                    )

                assert result.err is None and result.out is None
                if result.duration > self.problem.settings.timeout:
                    status = f'{Fore.RED}Aborted!'
                    config.n_error += 1
                elif not result.status and result.status != ExecStatus.TIMEOUT:
                    config.n_error += 1
                    status = None
                    print(
                        f'{Fore.RED}Run time error!{Style.RESET_ALL} exit code {result.returncode} {Style.BRIGHT}{result.duration:6.3f}s{Style.RESET_ALL}',
                        file=sys.stderr,
                    )
                elif (
                    result.duration > self.problem.settings.timelimit
                    or result.status == ExecStatus.TIMEOUT
                ):
                    status = f'{Fore.YELLOW}Done (TLE):'
                    config.n_warn += 1
                else:
                    status = f'{Fore.GREEN}Done:'

                if status:
                    print(
                        f'{status}{Style.RESET_ALL} {Style.BRIGHT}{result.duration:6.3f}s{Style.RESET_ALL}',
                        file=sys.stderr,
                    )
                print(file=sys.stderr)

            else:
                # Interactive problem.
                run = Run(self.problem, self, testcase)
                result = interactive.run_interactive_testcase(
                    run, interaction=True, validator_error=None, team_error=None
                )
                if result.verdict != 'ACCEPTED':
                    config.n_error += 1
                    print(
                        f'{Fore.RED}{result.verdict}{Style.RESET_ALL} {Style.BRIGHT}{result.duration:6.3f}s{Style.RESET_ALL}',
                        file=sys.stderr,
                    )
                else:
                    print(
                        f'{Fore.GREEN}{result.verdict}{Style.RESET_ALL} {Style.BRIGHT}{result.duration:6.3f}s{Style.RESET_ALL}',
                        file=sys.stderr,
                    )

    # Run the submission using stdin as input.
    def test_interactive(self):
        if self.problem.interactive:
            output_validators = self.problem.validators('output')
            if output_validators is False:
                return

        bar = ProgressBar('Running ' + str(self.name), max_len=1, count=1)
        bar.start()
        # print(ProgressBar.action('Running', str(self.name)), file=sys.stderr)

        is_tty = sys.stdin.isatty()

        tc = 0
        while True:
            tc += 1
            name = f'run {tc}'
            bar.update(1, len(name))
            bar.start(name)
            # Reinitialize the underlying program, so that changed to the source
            # code can be picked up in build.
            super().__init__(self.problem, self.path, skip_double_build_warning=True)
            bar.log('from stdin' if is_tty else 'from file')

            # Launch a separate thread to pass stdin to a pipe.
            r, w = os.pipe()
            ok = True
            eof = False

            TEE_CODE = R'''
import sys
while True:
    l = sys.stdin.read(1)
    if l=='': break
    sys.stdout.write(l)
    sys.stdout.flush()
'''
            writer = None

            # Wait for first input
            try:
                read = False
                for l in sys.stdin:
                    read = True
                    # Read the first line of input, and re-build the program if
                    # needed after the first line has been entered.
                    if not self.build(bar):
                        return
                    os.write(w, bytes(l, 'utf8'))
                    break
                if not read:
                    return

                writer = subprocess.Popen(['python3', '-c', TEE_CODE], stdin=None, stdout=w)

                assert self.run_command is not None
                result = exec_command(
                    self.run_command, crop=False, stdin=r, stdout=None, stderr=None, timeout=None
                )

                assert result.err is None and result.out is None
                if not result.status:
                    config.n_error += 1
                    status = None
                    print(
                        f'{Fore.RED}Run time error!{Style.RESET_ALL} exit code {result.returncode} {Style.BRIGHT}{result.duration:6.3f}s{Style.RESET_ALL}',
                        file=sys.stderr,
                    )
                else:
                    status = f'{Fore.GREEN}Done:'

                if status:
                    print(
                        f'{status}{Style.RESET_ALL} {Style.BRIGHT}{result.duration:6.3f}s{Style.RESET_ALL}',
                        file=sys.stderr,
                    )
                print(file=sys.stderr)
            finally:
                os.close(r)
                os.close(w)
                if writer:
                    writer.kill()
            bar.done()

            if not is_tty:
                break
