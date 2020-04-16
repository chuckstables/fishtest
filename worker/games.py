from __future__ import absolute_import
from __future__ import print_function

import datetime
import json
import os
import glob
import stat
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import traceback
import platform
import struct
import requests
from base64 import b64decode
from zipfile import ZipFile

try:
  from Queue import Queue, Empty
except ImportError:
  from queue import Queue, Empty  # python 3.x

# Global because is shared across threads
old_stats = {'wins':0, 'losses':0, 'draws':0, 'crashes':0, 'time_losses':0, 'pentanomial':5*[0]}

IS_WINDOWS = 'windows' in platform.system().lower()

def is_windows_64bit():
  if 'PROCESSOR_ARCHITEW6432' in os.environ:
    return True
  return os.environ['PROCESSOR_ARCHITECTURE'].endswith('64')

def is_64bit():
  if IS_WINDOWS:
    return is_windows_64bit()
  return '64' in platform.architecture()[0]

HTTP_TIMEOUT = 15.0

REPO_URL = 'https://github.com/official-stockfish/books'
ARCH = 'ARCH=x86-64-modern' if is_64bit() else 'ARCH=x86-32'
EXE_SUFFIX = ''
MAKE_CMD = 'make COMP=gcc ' + ARCH

if IS_WINDOWS:
  EXE_SUFFIX = '.exe'
  MAKE_CMD = 'make COMP=mingw ' + ARCH

def send_api_post_request(api_url, payload):
  return requests.post(api_url, data=json.dumps(payload), headers={
    'Content-Type': 'application/json'
  }, timeout=HTTP_TIMEOUT)

def github_api(repo):
  """ Convert from https://github.com/<user>/<repo>
      To https://api.github.com/repos/<user>/<repo> """
  return repo.replace('https://github.com', 'https://api.github.com/repos')

def enc(s):
  return s.encode('utf-8')

def verify_signature(engine, signature, remote, payload, concurrency):
  if concurrency > 1:
    busy_process = subprocess.Popen([engine], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    busy_process.stdin.write(enc('setoption name Threads value %d\n' % (concurrency-1)))
    busy_process.stdin.write(enc('go infinite\n'))
    busy_process.stdin.flush()

  try:
    bench_sig = ''
    print('Verifying signature of %s ...' % (os.path.basename(engine)))
    with open(os.devnull, 'wb') as f:
      p = subprocess.Popen([engine, 'bench'], stderr=subprocess.PIPE, stdout=f, universal_newlines=True)
    for line in iter(p.stderr.readline,''):
      if 'Nodes searched' in line:
        bench_sig = line.split(': ')[1].strip()
      if 'Nodes/second' in line:
        bench_nps = float(line.split(': ')[1].strip())

    p.wait()
    if p.returncode != 0:
      raise Exception('Bench exited with non-zero code %d' % (p.returncode))

    if int(bench_sig) != int(signature):
      message = 'Wrong bench in %s Expected: %s Got: %s' % (os.path.basename(engine), signature, bench_sig)
      payload['message'] = message
      send_api_post_request(remote + '/api/stop_run', payload)
      raise Exception(message)

  finally:
    if concurrency > 1:
      busy_process.communicate(enc('quit\n'))
      busy_process.stdin.close()

  return bench_nps

def setup(item, testing_dir):
  """Download item from FishCooking to testing_dir"""
  tree = requests.get(github_api(REPO_URL) + '/git/trees/master', timeout=HTTP_TIMEOUT).json()
  for blob in tree['tree']:
    if blob['path'] == item:
      print('Downloading %s ...' % (item))
      blob_json = requests.get(blob['url'], timeout=HTTP_TIMEOUT).json()
      with open(os.path.join(testing_dir, item), 'wb+') as f:
        f.write(b64decode(blob_json['content']))
      break
  else:
    raise Exception('Item %s not found' % (item))

def setup_engine(destination, worker_dir, sha, repo_url, concurrency):
  if os.path.exists(destination): os.remove(destination)
  """Download and build sources in a temporary directory then move exe to destination"""
  tmp_dir = tempfile.mkdtemp(dir=worker_dir)

  try:
    os.chdir(tmp_dir)
    with open('sf.gz', 'wb+') as f:
      f.write(requests.get(github_api(repo_url) + '/zipball/' + sha, timeout=HTTP_TIMEOUT).content)
    zip_file = ZipFile('sf.gz')
    zip_file.extractall()
    zip_file.close()

    for name in zip_file.namelist():
      if name.endswith('/src/'):
        src_dir = name
    os.chdir(src_dir)

    custom_make = os.path.join(worker_dir, 'custom_make.txt')
    if os.path.exists(custom_make):
      with open(custom_make, 'r') as m:
        make_cmd = m.read().strip()
      subprocess.check_call(make_cmd, shell=True)
    else:
      subprocess.check_call(MAKE_CMD + ' -j %s' % (concurrency) + ' profile-build', shell=True)
      try: # try/pass needed for backwards compatibility with older stockfish, where 'make strip' fails under mingw.
        subprocess.check_call(MAKE_CMD + ' -j %s' % (concurrency) + ' strip', shell=True)
      except:
        pass


    shutil.move('stockfish'+ EXE_SUFFIX, destination)
  except:
    raise Exception('Failed to setup engine for %s' % (sha))
  finally:
    os.chdir(worker_dir)
    shutil.rmtree(tmp_dir)

def kill_process(p):
  try:
    if IS_WINDOWS:
      # Kill doesn't kill subprocesses on Windows
      subprocess.call(['taskkill', '/F', '/T', '/PID', str(p.pid)])
    else:
      p.kill()
    p.wait()
    p.stdout.close()
  except:
    print('Note: ' + str(sys.exc_info()[0]) + ' killing the process pid: ' + str(p.pid) + ', possibly already terminated')
    pass

def adjust_tc(tc, base_nps, concurrency):
  factor = 1600000.0 / base_nps
  if base_nps < 700000:
    sys.stderr.write('This machine is too slow to run fishtest effectively - sorry!\n')
    sys.exit(1)

  # Parse the time control in cutechess format
  chunks = tc.split('+')
  increment = 0.0
  if len(chunks) == 2:
    increment = float(chunks[1])

  chunks = chunks[0].split('/')
  num_moves = 0
  if len(chunks) == 2:
    num_moves = int(chunks[0])

  time_tc = chunks[-1]
  chunks = time_tc.split(':')
  if len(chunks) == 2:
    time_tc = float(chunks[0]) * 60 + float(chunks[1])
  else:
    time_tc = float(chunks[0])

  # Rebuild scaled_tc now
  scaled_tc = '%.3f' % (time_tc * factor)
  tc_limit = time_tc * factor * 3
  if increment > 0.0:
    scaled_tc += '+%.3f' % (increment * factor)
    tc_limit += increment * factor * 200
  if num_moves > 0:
    scaled_tc = '%d/%s' % (num_moves, scaled_tc)
    tc_limit *= 100.0 / num_moves

  print('CPU factor : %f - tc adjusted to %s' % (factor, scaled_tc))
  return scaled_tc, tc_limit

def enqueue_output(out, queue):
  try:
    for line in iter(out.readline, b''):
      queue.put(line)
    out.close()
  except: # Happens on closed file/killed process
    return

w_params = None
b_params = None


def update_pentanomial(line,rounds):
  def result_to_score(_result):
    if _result=="1-0":
      return 2
    elif _result=="0-1":
      return 0
    elif _result=="1/2-1/2":
      return 1
    else:
      return -1
  if not 'pentanomial' in rounds.keys():
    rounds['pentanomial']=5*[0]
  if not 'trinomial' in rounds.keys():
    rounds['trinomial']=3*[0]
  # Parse line like this:
  # Finished game 4 (Base-5446e6f vs New-1a68b26): 1/2-1/2 {Draw by adjudication}
  line=line.split()
  if line[0]=='Finished' and line[1]=='game' and len(line)>=7:
    round_=int(line[2])
    current={}
    rounds[round_]=current
    current['white']=line[3][1:]
    current['black']=line[5][:-2]
    i=current['result']=result_to_score(line[6])
    if round_%2==0:
      if i!=-1:
        rounds['trinomial'][2-i]+=1   #reversed colors
      odd=round_-1
      even=round_
    else:
      if i!=-1:
        rounds['trinomial'][i]+=1
      odd=round_
      even=round_+1
    if odd in rounds.keys() and even in rounds.keys():
      assert(rounds[odd]['white'][0:3]=='New')
      assert(rounds[odd]['white']==rounds[even]['black'])
      assert(rounds[odd]['black']==rounds[even]['white'])
      i=rounds[odd]['result']
      j=rounds[even]['result']  # even is reversed colors
      if i!=-1 and j!=-1:
        rounds['pentanomial'][i+2-j]+=1
        del rounds[odd]
        del rounds[even]
        rounds['trinomial'][i]-=1
        rounds['trinomial'][2-j]-=1
        assert(rounds['trinomial'][i]>=0)
        assert(rounds['trinomial'][2-j]>=0)
  pentanomial=[rounds['pentanomial'][i]+old_stats['pentanomial'][i] for i in range(0,5)]
  return pentanomial

def validate_pentanomial(wld, rounds):
  def results_to_score(results):
    return sum([results[i] * (i / 2.0) for i in range(0, len(results))])
  LDW = [wld[1], wld[2], wld[0]]
  s3 = results_to_score(LDW)
  s5 = results_to_score(rounds['pentanomial']) + results_to_score(rounds['trinomial'])
  assert(sum(LDW) == 2 * sum(rounds['pentanomial']) + sum(rounds['trinomial']))
  epsilon = 1e-4
  assert(abs(s5 - s3) < epsilon)


def run_game(p, remote, result, spsa, spsa_tuning, tc_limit):
  global old_stats
  rounds = {}

  q = Queue()
  t = threading.Thread(target=enqueue_output, args=(p.stdout, q))
  t.daemon = True
  t.start()

  end_time = datetime.datetime.now() + datetime.timedelta(seconds=tc_limit)
  print('TC limit {} End time: {}'.format(tc_limit, end_time))

  num_game_pairs_updated = 0
  while datetime.datetime.now() < end_time:
    try:
      line = q.get_nowait()
    except Empty:
      if p.poll():
        break
      time.sleep(1)
      continue

    sys.stdout.write(line)
    sys.stdout.flush()

    # Have we reached the end of the match?  Then just exit
    if 'Finished match' in line:
      print('Finished match cleanly')
      kill_process(p)
      return { 'task_alive': True }

    # Parse line like this:
    # Finished game 1 (stockfish vs base): 0-1 {White disconnects}
    if 'disconnects' in line or 'connection stalls' in line:
      result['stats']['crashes'] += 1

    if 'on time' in line:
      result['stats']['time_losses'] += 1

    # Parse line like this:
    # Score of stockfish vs base: 0 - 0 - 1  [0.500] 1
    if 'Score' in line:
      chunks = line.split(':')
      chunks = chunks[1].split()
      wld = [int(chunks[0]), int(chunks[2]), int(chunks[4])]

      validate_pentanomial(wld, rounds)

      if not spsa_tuning:
        result['stats']['wins']   = wld[0] - rounds['trinomial'][2] + old_stats['wins']
        result['stats']['losses'] = wld[1] - rounds['trinomial'][0] + old_stats['losses']
        result['stats']['draws']  = wld[2] - rounds['trinomial'][1] + old_stats['draws']
      else:
        result['stats']['wins']   = wld[0] + old_stats['wins']
        result['stats']['losses'] = wld[1] + old_stats['losses']
        result['stats']['draws']  = wld[2] + old_stats['draws']
        spsa['wins'] = wld[0]
        spsa['losses'] = wld[1]
        spsa['draws'] = wld[2]

      num_game_pairs_finished = sum(rounds['pentanomial'])
      # Send an update_task request after a game pair finishes, not after every game
      if num_game_pairs_finished > num_game_pairs_updated:
        # Attempt to send game results to the server. Retry a few times upon error
        update_succeeded = False
        for _ in range(0, 5):
          try:
            t0 = datetime.datetime.utcnow()
            response = send_api_post_request(remote + '/api/update_task', result).json()
            print("  Task updated successfully in %ss" % ((datetime.datetime.utcnow() - t0).total_seconds()))
            if not response['task_alive']:
              # This task is no longer neccesary
              print('Server told us task is no longer needed')
              kill_process(p)
              return response
            update_succeeded = True
            break
          except Exception as e:
            sys.stderr.write('Exception from calling update_task:\n')
            print(e)
            # traceback.print_exc(file=sys.stderr)
          time.sleep(HTTP_TIMEOUT)
        if not update_succeeded:
          print('Too many failed update attempts')
          kill_process(p)
          break
        num_game_pairs_updated = num_game_pairs_finished

    pentanomial = update_pentanomial(line, rounds)
    result['stats']['pentanomial'] = pentanomial

  now = datetime.datetime.now()
  if now >= end_time:
    print('{} is past end time {}'.format(now, end_time))
  kill_process(p)
  return { 'task_alive': True }

def launch_cutechess(cmd, remote, result, spsa_tuning, games_to_play, tc_limit):
  spsa = {
    'w_params': [],
    'b_params': [],
    'num_games': games_to_play,
  }

  if spsa_tuning:
    # Request parameters for next game
    t0 = datetime.datetime.utcnow()
    req = send_api_post_request(remote + '/api/request_spsa', result).json()
    print("Fetched SPSA parameters successfully in %ss" % ((datetime.datetime.utcnow() - t0).total_seconds()))

    global w_params, b_params
    w_params = req['w_params']
    b_params = req['b_params']

    result['spsa'] = spsa
  else:
    w_params = []
    b_params = []

  # Run cutechess-cli binary
  idx = cmd.index('_spsa_')
  cmd = cmd[:idx] + ['option.%s=%d'%(x['name'], round(x['value'])) for x in w_params] + cmd[idx+1:]
  idx = cmd.index('_spsa_')
  cmd = cmd[:idx] + ['option.%s=%d'%(x['name'], round(x['value'])) for x in b_params] + cmd[idx+1:]

  print(cmd)
  p = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True, bufsize=1, close_fds=not IS_WINDOWS)

  try:
    return run_game(p, remote, result, spsa, spsa_tuning, tc_limit)
  except Exception as e:
    traceback.print_exc(file=sys.stderr)
    try:
      print('Exception running games')
      kill_process(p)
    except:
      pass

  return { 'task_alive': False }

def run_games(worker_info, password, remote, run, task_id):
  task = run['my_task']
  result = {
    'username': worker_info['username'],
    'password': password,
    'run_id': str(run['_id']),
    'task_id': task_id,
    'stats': {'wins':0, 'losses':0, 'draws':0, 'crashes':0, 'time_losses':0, 'pentanomial':5*[0]},
  }

  # Have we run any games on this task yet?
  global old_stats
  old_stats = task.get('stats', {'wins':0, 'losses':0, 'draws':0, 'crashes':0, 'time_losses':0, 'pentanomial':5*[0]})
  if not 'pentanomial' in old_stats.keys():
    old_stats['pentanomial']=5*[0]
  result['stats']['crashes'] = old_stats.get('crashes', 0)
  result['stats']['time_losses'] = old_stats.get('time_losses', 0)
  games_remaining = task['num_games'] - (old_stats['wins'] + old_stats['losses'] + old_stats['draws'])
  if games_remaining <= 0:
    raise Exception('No games remaining')

  book = run['args']['book']
  book_depth = run['args']['book_depth']
  new_options = run['args']['new_options']
  base_options = run['args']['base_options']
  threads = int(run['args']['threads'])
  spsa_tuning = 'spsa' in run['args']
  repo_url = run['args'].get('tests_repo', REPO_URL)
  games_concurrency = int(worker_info['concurrency']) / threads

  # Format options according to cutechess syntax
  def parse_options(s):
    results = []
    chunks = s.split('=')
    if len(chunks) == 0:
      return results
    param = chunks[0]
    for c in chunks[1:]:
      val = c.split()
      results.append('option.%s=%s' % (param, val[0]))
      param = ' '.join(val[1:])
    return results

  new_options = parse_options(new_options)
  base_options = parse_options(base_options)

  # Setup testing directory if not already exsisting
  worker_dir = os.path.dirname(os.path.realpath(__file__))
  testing_dir = os.path.join(worker_dir, 'testing')
  if not os.path.exists(testing_dir):
    os.makedirs(testing_dir)

  # clean up old engines (keeping the 25 most recent)
  engines = glob.glob(os.path.join(testing_dir, 'stockfish_*' + EXE_SUFFIX))
  if len(engines) > 25:
    engines.sort(key=os.path.getmtime)
    for old_engine in engines[:len(engines)-25]:
      try:
         os.remove(old_engine)
      except:
         print('Note: failed to remove an old engine binary ' + str(old_engine))
         pass

  # create new engines
  sha_new = run['args']['resolved_new']
  sha_base = run['args']['resolved_base']
  new_engine_name = 'stockfish_' + sha_new
  base_engine_name = 'stockfish_' + sha_base

  new_engine = os.path.join(testing_dir, new_engine_name + EXE_SUFFIX)
  base_engine = os.path.join(testing_dir, base_engine_name + EXE_SUFFIX)
  cutechess = os.path.join(testing_dir, 'cutechess-cli' + EXE_SUFFIX)

  # Build from sources new and base engines as needed
  if not os.path.exists(new_engine):
    setup_engine(new_engine, worker_dir, sha_new, repo_url, worker_info['concurrency'])
  if not os.path.exists(base_engine):
    setup_engine(base_engine, worker_dir, sha_base, repo_url, worker_info['concurrency'])

  os.chdir(testing_dir)

  # Download book if not already existing
  if not os.path.exists(os.path.join(testing_dir, book)) or os.stat(os.path.join(testing_dir, book)).st_size == 0:
    zipball = book + '.zip'
    setup(zipball, testing_dir)
    zip_file = ZipFile(zipball)
    zip_file.extractall()
    zip_file.close()
    os.remove(zipball)

  # Download cutechess if not already existing
  if not os.path.exists(cutechess):
    if len(EXE_SUFFIX) > 0: zipball = 'cutechess-cli-win.zip'
    else: zipball = 'cutechess-cli-linux-%s.zip' % (platform.architecture()[0])
    setup(zipball, testing_dir)
    zip_file = ZipFile(zipball)
    zip_file.extractall()
    zip_file.close()
    os.remove(zipball)
    os.chmod(cutechess, os.stat(cutechess).st_mode | stat.S_IEXEC)

  pgn_name = 'results-' + worker_info['unique_key'] + '.pgn'
  if os.path.exists(pgn_name):
    os.remove(pgn_name)
  pgnfile = os.path.join(testing_dir, pgn_name)

  # Verify signatures are correct
  verify_signature(new_engine, run['args']['new_signature'], remote, result, games_concurrency * threads)
  base_nps = verify_signature(base_engine, run['args']['base_signature'], remote, result, games_concurrency * threads)

  # Benchmark to adjust cpu scaling
  scaled_tc, tc_limit = adjust_tc(run['args']['tc'], base_nps, int(worker_info['concurrency']))
  result['nps'] = base_nps

  # Handle book or pgn file
  pgn_cmd = []
  book_cmd = []
  if int(book_depth) <= 0:
    pass
  elif book.endswith('.pgn') or book.endswith('.epd'):
    plies = 2 * int(book_depth)
    pgn_cmd = ['-openings', 'file=%s' % (book), 'format=%s' % (book[-3:]), 'order=random', 'plies=%d' % (plies)]
  else:
    book_cmd = ['book=%s' % (book), 'bookdepth=%s' % (book_depth)]

  print('Running %s vs %s' % (run['args']['new_tag'], run['args']['base_tag']))

  if spsa_tuning:
    games_to_play = games_concurrency * 2
    tc_limit *= 2
    pgnout = []
  else:
    games_to_play = games_remaining
    pgnout = ['-pgnout', pgn_name]

  threads_cmd=[]
  if not any("Threads" in s for s in new_options + base_options):
    threads_cmd = ['option.Threads=%d' % (threads)]

  # If nodestime is being used, give engines extra grace time to
  # make time losses virtually impossible
  nodestime_cmd=[]
  if any ("nodestime" in s for s in new_options + base_options):
    nodestime_cmd = ['timemargin=10000']

  def make_player(arg):
    return run['args'][arg].split(' ')[0]

  while games_remaining > 0:
    # Run cutechess-cli binary
    cmd = [ cutechess, '-repeat', '-rounds', str(int(games_to_play/2)), '-games', ' 2', '-tournament', 'gauntlet'] + pgnout + \
          ['-site', 'https://tests.stockfishchess.org/tests/view/' + run['_id']] + \
          ['-event', 'Batch %d: %s vs %s' % (task_id, make_player('new_tag'), make_player('base_tag'))] + \
          ['-srand', "%d" % struct.unpack("<L", os.urandom(struct.calcsize("<L")))] + \
          ['-resign', 'movecount=3', 'score=400', '-draw', 'movenumber=34',
           'movecount=8', 'score=20', '-concurrency', str(int(games_concurrency))] + pgn_cmd + \
          ['-engine', 'name=New-'+run['args']['resolved_new'][:7], 'cmd=%s' % (new_engine_name)] + new_options + ['_spsa_'] + \
          ['-engine', 'name=Base-'+run['args']['resolved_base'][:7], 'cmd=%s' % (base_engine_name)] + base_options + ['_spsa_'] + \
          ['-each', 'proto=uci', 'tc=%s' % (scaled_tc)] + nodestime_cmd + threads_cmd + book_cmd

    task_status = launch_cutechess(cmd, remote, result, spsa_tuning, games_to_play,
                                   tc_limit * games_to_play / min(games_to_play, games_concurrency))
    if not task_status.get('task_alive', False):
      break

    old_stats = result['stats'].copy()
    games_remaining -= games_to_play

  return pgnfile
