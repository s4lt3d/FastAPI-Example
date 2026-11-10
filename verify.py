# Run the background-job API checks.
import runpy
runpy.run_path(str(__import__('pathlib').Path(__file__).with_name('verify_jobs.py')), run_name='__main__')
