import cairosvg
from PIL import Image
import io
import numpy as np
import uuid
import pandas as pd
import os,sys
from art import tprint

sys.path.append('..')
import model_discovery.utils as U
import pytz
from datetime import datetime, timedelta

from model_discovery.configs.const import NODE_ZOMBIE_THRESHOLD

CLI_TITLE = 'GENESYS'



def print_cli_title():
    tprint(CLI_TITLE, font='modular')
    print('Welcome to the Model Discovery System!')

def get_setting(): 
    ckpt_dir = os.environ.get('CKPT_DIR')
    setting_dir=U.pjoin(ckpt_dir,'.setting.json')
    setting=U.load_json(setting_dir)
    return setting
  
def save_setting(setting):
    ckpt_dir = os.environ.get('CKPT_DIR')
    setting_dir=U.pjoin(ckpt_dir,'.setting.json')
    U.save_json(setting,setting_dir)
  
def theme_aware_options(st,option_light,option_dark,default):
  if st.session_state.current_theme:
    return option_light if st.session_state.current_theme['base']=='light' else option_dark
  else:
    return default


SQUARE_LOGO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
    <path d="M50,50 L250,50 L250,250 L50,250 Z" fill="none" stroke="#{COLOR}" stroke-width="4" />
    <text x="150" y="140" font-family="Arial, sans-serif" font-size="60" font-weight="bold" fill="#{COLOR}" text-anchor="middle">{UPPER_TEXT}</text>
    <text x="150" y="210" font-family="Arial, sans-serif" font-size="60" font-weight="bold" fill="#{COLOR}" text-anchor="middle">{LOWER_TEXT}</text>
</svg>
"""

SQUARE_CENTER_LOGO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
    <path d="M50,50 L250,50 L250,250 L50,250 Z" fill="none" stroke="#{COLOR}" stroke-width="4" />
    <text x="150" y="165" font-family="Arial, sans-serif" font-size="60" font-weight="bold" fill="#{COLOR}" text-anchor="middle">{UPPER_TEXT}</text>
</svg>
"""
# font-style="italic" 

def square_logo(upper_text, lower_text=None, color='000000'):
    if lower_text is None:
        svg_code = SQUARE_CENTER_LOGO_SVG.format(UPPER_TEXT=upper_text, COLOR=color)
    else:
        svg_code = SQUARE_LOGO_SVG.format(UPPER_TEXT=upper_text, LOWER_TEXT=lower_text, COLOR=color)
    png_data = cairosvg.svg2png(bytestring=svg_code.encode('utf-8'))
    return Image.open(io.BytesIO(png_data))

def svg_to_image(svg_path):
    svg_data = open(svg_path, 'r').read()
    png_data = cairosvg.svg2png(bytestring=svg_data.encode('utf-8'))
    return Image.open(io.BytesIO(png_data))


def grid_view(st,item_dict:dict,per_row=3,spacing=0.05):
    n_items=len(item_dict)
    n_rows = int(np.ceil(len(item_dict)/per_row))
    for i in range(n_rows):
        col_widths=np.ones(per_row*2-1)
        col_widths[1::2]=spacing
        cols=st.columns(col_widths)
        n_cols=min(per_row,n_items-i*per_row)
        for j in list(range(n_cols)):
          with cols[j*2]:
            index=i*per_row+j
            title,value=list(item_dict.items())[index]
            icon=value.pop('ICON',None)
            with st.expander(title,expanded=True,icon=icon):
              if 'BUTTON' in value:
                buttons=value.pop('BUTTON')
                st.write(value)
                if len(buttons)==1:
                  text,fn,disabled=buttons[0]
                  st.button(text,on_click=fn,disabled=disabled,key=str(uuid.uuid4()))
                else:
                  btn_cols=st.columns(max(3,len(buttons)))
                  for j in range(len(buttons)):
                    text,fn,disabled=buttons[j]
                    with btn_cols[j]:
                      st.button(text,on_click=fn,disabled=disabled,key=str(uuid.uuid4()),use_container_width=True)
              else:
                st.write(value)


def _listener_running(zombie_threshold=NODE_ZOMBIE_THRESHOLD):
    with U.local_lock(5):
      local_doc = U.read_local_doc()
      if local_doc:
          if local_doc.get('status',None) == 'stopped':
              return None
          last_heartbeat = datetime.fromisoformat(local_doc['last_heartbeat'])
          threshold_time = datetime.now(pytz.UTC) - timedelta(seconds=zombie_threshold)
          if last_heartbeat > threshold_time:
              return local_doc['node_id']
          else:
              local_doc['status'] = 'stopped'
              U.write_local_doc(local_doc)
    return None

def _refresh_local_listener_status(st):
  _node_id = _listener_running()
  if st.session_state.listener:
    if _node_id:
      st.session_state.listener.wake_up(_node_id)
    else:
      st.toast(f'Local running listener not running anymore. Stopping the listener...')
      st.session_state.listener.hanging()



def system_status(st,evosys,title,icon):
  with st.expander(f"{title}",expanded=True,icon=icon):
      settings={}
      settings['Experiment Directory']=evosys.evo_dir
      
      if st.session_state.use_cache:
        n_implemented = len(st.session_state.implemented_designs)
        n_designs = n_implemented + len(st.session_state.unimplemented_designs)
      else:
        n_implemented = len(evosys.ptree.filter_by_type('DesignArtifactImplemented'))
        n_designs = n_implemented + len(evosys.ptree.filter_by_type('DesignArtifact'))

      st.write(f'Designs: :blue[{n_designs}] (**:green[{n_implemented}]**)')
      design_cost = evosys.ptree.design_cost if not st.session_state.is_demo else 99999.99
      if evosys.design_budget_limit>0:
          text=f'💲: {design_cost:.2f}/{evosys.design_budget_limit:.2f}'
          st.progress(min(1.0,design_cost/evosys.design_budget_limit),text=text)
      else:
          text=f'💲: {design_cost:.2f}/♾️'
          st.progress(1.0,text=text)

      _verify_budget = U.sort_dict_by_scale(evosys.selector._verify_budget,False)
      for scale,num in _verify_budget.items():
        finished = num-evosys.selector.verify_budget[scale] 
        if st.session_state.is_demo and finished == 0:
            continue
        text=f'{scale}: {finished}/{num}'
        st.progress(min(1.0,finished/num),text=text)
      st.write(f'Budget Type: ```{evosys.params["budget_type"]}```')


def daily_usage_status(st, show = False):
  daily_usage = U.get_daily_usage(total=True)
  _status = min(1.0,daily_usage/st.session_state.daily_usage_limit)
  if show:
    st.progress(_status,text=f'💰 :orange[*Daily limit:*] ```{daily_usage:.2f}```/```{st.session_state.daily_usage_limit:.2f}```')
  return _status

def running_status(st,evosys):
  db_status = f'📶' if evosys.ptree.remote_db else '📴'
  st.write(f'🏠 **Namespace\n```{evosys.evoname}``` {db_status}**')
  # if evosys.remote_db:
  #   URL='https://console.firebase.google.com/u/0/project/model-discovery/firestore/databases/-default-/data'
  #   st.write(f'⛅ [**Cloud Status**]({URL})')

  if st.session_state.is_demo:
    daily_usage_status(st,show=True)

  if st.session_state.evo_running:
    if evosys.benchmark_mode:
      title='***Bench Running***'
      icon='🪑'
    else:
      title='***Evo Running***'
      icon='🚀'
    system_status(st,evosys,title,icon)
 
  if evosys.CM is not None:
    evosys.CM.get_active_connections()
    active_connections = evosys.CM.connections
    with st.expander(f"🌐 Connections: ```{len(active_connections)}```",expanded=False):
      st.write(f'***Group ID:***\n```{evosys.CM.group_id}```')
      if len(active_connections)!=0:
        for node_id in active_connections:
          _running_designs, _running_verifies = evosys.CM.check_workload(node_id)
          _max_designs = evosys.CM.max_design_threads[node_id]
          _max_verifies = 1 if evosys.CM.accept_verify_job[node_id] else 0
          _design_status = f'{len(_running_designs)}/{_max_designs}🐎' if _max_designs>0 else 'N/A🐎'
          _verify_status = f'{len(_running_verifies)}/{_max_verifies}🥏' if _max_verifies>0 else 'N/A🥏'
          st.write(f'```{node_id}```{_design_status}{_verify_status}')
      else:
        st.info('No active connections')
  else:
     if st.session_state.is_demo:
        with st.expander(f"🌐 Connections: ```0```",expanded=False):
          st.write(f'***Group ID:***\n```null```')
          st.info('Offline, no connections.')
          


  running_verifications=[key for key,process in st.session_state.get('running_verifications',{}).items() if process.poll() is None]
  
  if st.session_state.listening_mode and st.session_state.listener.node_id:
    st.divider()

    _refresh_local_listener_status(st)

  if st.session_state.listening_mode and st.session_state.listener.node_id:
    st.status(f'👂```{st.session_state.listener.node_id}```*listening*\n')
  
  if st.session_state.listening_mode and st.session_state.listener.node_id:
    with st.expander("🥏 Running Verifies",expanded=False):
      if len(running_verifications)!=0:
        for idx,key in enumerate(running_verifications):
          st.write(f'{idx+1}. ```{key}```')
      else:
        st.info('No running verifications')
    with st.expander("🐎 Running Designs",expanded=False):
      running_designs,raw_sess_data=st.session_state.listener.get_running_design_sessions(ret_raw=True)
      if len(running_designs)!=0:
        data={}
        for sess_id in running_designs:
            status = raw_sess_data[sess_id].get('status','N/A')
            data[sess_id] = {'status':status}
        df = pd.DataFrame(data,columns=['Status'])
        st.dataframe(df,use_container_width=True,hide_index=True)
      else:
        st.info('No running designs')
  
  if not st.session_state.evo_running and not st.session_state.listening_mode:
    st.info(':gray[*No workloads*] 💤')
  
  st.divider()
    
            
          
  