import traceback
from flask import Flask, jsonify, make_response, request, Response
from flask_compress import Compress
import psycopg2
import pandas as pd, numpy as np
import argparse
import json
import requests
import time
import itertools
import os, threading
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from typing import Any, Tuple, List, Dict, Optional, Union

import calculate_services_cnt
import experimental_aggregation
from thread_pool import ThreadPool

class Properties:
    def __init__(
            self, provision_db_addr: str, provision_db_port: int, provision_db_name: str, provision_db_user: str, provision_db_pass: str,
            houses_db_addr: str, houses_db_port: int, houses_db_name: str, houses_db_user: str, houses_db_pass: str,
            api_port: int, transport_model_api_endpoint: str):
        self.provision_db_addr = provision_db_addr
        self.provision_db_port = provision_db_port
        self.provision_db_name = provision_db_name
        self.provision_db_user = provision_db_user
        self.provision_db_pass = provision_db_pass
        self.houses_db_addr = houses_db_addr
        self.houses_db_port = houses_db_port
        self.houses_db_name = houses_db_name
        self.houses_db_user = houses_db_user
        self.houses_db_pass = houses_db_pass
        self.api_port = api_port
        self.transport_model_api_endpoint = transport_model_api_endpoint
        self._houses_conn: Optional[psycopg2.extensions.connection] = None
        self._provision_conn: Optional[psycopg2.extensions.connection] = None
    @property
    def provision_conn_string(self) -> str:
        return f'host={self.provision_db_addr} port={self.provision_db_port} dbname={self.provision_db_name}' \
                f' user={self.provision_db_user} password={self.provision_db_pass}'
    @property
    def houses_conn_string(self) -> str:
        return f'host={self.houses_db_addr} port={self.houses_db_port} dbname={self.houses_db_name}' \
                f' user={self.houses_db_user} password={self.houses_db_pass}'
    @property
    def houses_conn(self) -> psycopg2.extensions.connection:
        if self._houses_conn is None or self._houses_conn.closed:
            self._houses_conn = psycopg2.connect(self.houses_conn_string)
        return self._houses_conn
            
    @property
    def provision_conn(self) -> psycopg2.extensions.connection:
        if self._provision_conn is None or self._provision_conn.closed:
            self._provision_conn = psycopg2.connect(self.provision_conn_string)
        return self._provision_conn

    def close(self):
        if self.houses_conn is not None:
            self._houses_conn.close()
        if self._provision_conn is not None:
            self._provision_conn.close()

class Avaliability:
    def get_walking(self, lat: float, lan: float, t: int, pipe: Optional[Connection] = None) -> str:
        if t == 0:
            ans = json.dumps({
                    'type': 'Polygon',
                    'coordinates': []
            })
            if pipe is not None:
                pipe.send(ans)
            return ans
        with properties.provision_conn.cursor() as cur:
            cur.execute(f'select ST_AsGeoJSON(geometry) from walking where latitude = {lat} and longitude = {lan} and time = {t}')
            res = cur.fetchall()
            if len(res) != 0:
                if pipe is not None:
                    pipe.send(res[0][0])
                return res[0][0]
            try:
                print(f'downloading walking for {lat}, {lan}, {t}')
                ans = json.dumps(
                    requests.get(f'https://galton.urbica.co/api/foot/?lng={lat}&lat={lan}&radius=5&cellSize=0.1&intervals={t}', timeout=15).json()['features'][0]['geometry']
                )
            except Exception:
                ans = json.dumps({'type': 'Polygon', 'coordinates': []})
                properties.provision_conn.rollback()
                if pipe is not None:
                    pipe.send(ans)
                return ans

            try:
                cur.execute(f"INSERT INTO walking (latitude, longitude, time, geometry) VALUES ({lat}, {lan}, {t}, ST_SetSRID(ST_GeomFromGeoJSON('{ans}'::text), 4326)) ON CONFLICT DO NOTHING")
                properties.provision_conn.commit()
            except Exception:
                properties.provision_conn.rollback()
                
        if pipe is not None:
            pipe.send(ans)
        return ans


    def get_transport(self, lat: float, lan: float, t: int, pipe: Optional[Connection] = None) -> str:
        if t == 0:
            res = json.dumps({
                'type': 'Polygon',
                'coordinates': []
            })
            if pipe is not None:
                pipe.send(res)
            return res
        with properties.provision_conn.cursor() as cur:
            cur.execute(f'SELECT ST_AsGeoJSON(geometry) FROM transport where latitude = {lat} and longitude = {lan} and time = {t}')
            res = cur.fetchall()
            if len(res) != 0:
                if pipe is not None:
                    pipe.send(res[0][0])
                return res[0][0]
            if t >= 60:
                cur.execute(f'SELECT ST_AsGeoJSON(geometry) FROM transport where time = {t} limit 1')
                res = cur.fetchall()
                if len(res) != 0:
                    if pipe is not None:
                        pipe.send(res[0][0])
                    return res[0][0]
            print(f'downloading transport for {lat}, {lan}, {t}')
            try:
                data = requests.post(f'{properties.transport_model_api_endpoint}', timeout=15, json=
                    {
                        'source': [lan, lat],
                        'cost': t * 60,
                        'day_time': 46800,
                        'mode_type': 'pt_cost'
                    }
                ).json()
                if len(data['features']) == 0:
                    ans = json.dumps({'type': 'Polygon', 'coordinates': []})
                else:
                    cur.execute('SELECT ST_AsGeoJSON(ST_UNION(ARRAY[' + ',\n'.join(map(lambda x: f'ST_GeomFromGeoJSON(\'{json.dumps(x["geometry"])}\')', data['features'])) + ']))')
                    ans = cur.fetchall()[0][0]
            except Exception:
                ans = json.dumps({'type': 'Polygon', 'coordinates': []})
                properties.provision_conn.rollback()
                if pipe is not None:
                    pipe.send(ans)
                return ans

            try:
                cur.execute('INSERT INTO transport (latitude, longitude, time, geometry) VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326)) ON CONFLICT DO NOTHING', (lat, lan, t, ans))
                properties.provision_conn.commit()
            except Exception:
                properties.provision_conn.rollback()
            if pipe is not None:
                pipe.send(ans)
        return ans

    def get_car(self, lat: float, lan: float, t: int, pipe: Optional[Connection] = None) -> str:
        if t == 0:
            res = json.dumps({
                'type': 'Polygon',
                'coordinates': []
            })
            if pipe is not None:
                pipe.send(res)
            return res
        with properties.provision_conn.cursor() as cur:
            cur.execute(f'select ST_AsGeoJSON(geometry) from car where latitude = {lat} and longitude = {lan} and time = {t}')
            res = cur.fetchall()
            if len(res) != 0:
                if pipe is not None:
                    pipe.send(res[0][0])
                return res[0][0]
            if t >= 60:
                cur.execute(f'select ST_AsGeoJSON(geometry) from car where time = {t} limit 1')
                res = cur.fetchall()
                if len(res) != 0:
                    if pipe is not None:
                        pipe.send(res[0][0])
                    return res[0][0]
            print(f'downloading car for {lat}, {lan}, {t}')
            try:
                data = requests.post(f'{properties.transport_model_api_endpoint}', timeout=15, json=
                    {
                        'source': [lan, lat],
                        'cost': t * 60,
                        'day_time': 46800,
                        'mode_type': 'car_cost'
                    }
                ).json()
                if len(data['features']) == 0:
                    ans = json.dumps({'type': 'Polygon', 'coordinates': []})
                else:
                    cur.execute('SELECT ST_AsGeoJSON(ST_UNION(ARRAY[' + ',\n'.join(map(lambda x: f'ST_GeomFromGeoJSON(\'{json.dumps(x["geometry"])}\')', data['features'])) + ']))')
                    ans = cur.fetchall()[0][0]
            except Exception:
                ans = json.dumps({'type': 'Polygon', 'coordinates': []})
                properties.provision_conn.rollback()
                if pipe is not None:
                    pipe.send(ans)
                return ans

            try:
                cur.execute('INSERT INTO car (latitude, longitude, time, geometry) VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326)) ON CONFLICT DO NOTHING', (lat, lan, t, ans))
                properties.provision_conn.commit()
            except Exception:
                properties.provision_conn.rollback()
        if pipe is not None:
            pipe.send(ans)
        return ans
    
    def ensure_ready(self, lat: float, lan: float, time_walking: int, time_transport: int, time_car: int) -> Tuple[str, str, str]:
        pipes = [Pipe() for _ in range(3)]
        threads = list(map(lambda func_and_t: threading.Thread(target=lambda: func_and_t[0](lat, lan, func_and_t[1], func_and_t[2])),
                ((self.get_walking, time_walking, pipes[0][0]), (self.get_transport, time_transport, pipes[1][0]), (self.get_car, time_car, pipes[2][0]))))

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()
        
        return tuple([pipe[1].recv() for pipe in pipes]) # type: ignore

properties: Properties
avaliability: Avaliability

needs: pd.DataFrame
all_houses: pd.DataFrame
infrastructure: pd.DataFrame
services_buildings: pd.DataFrame
blocks: pd.DataFrame
city_hierarchy: pd.DataFrame

def compute_atomic_provision(social_group: str, living_situation: str, service: str, coords: Tuple[float, float],
        provision_conn: psycopg2.extensions.connection, houses_conn: psycopg2.extensions.connection, **kwargs) -> Dict[str, Any]:
    walking_time_cost: int
    transport_time_cost: int
    personal_transport_time_cost: int
    intensity: float
    significance: float
    walking_time_cost, transport_time_cost, personal_transport_time_cost, intensity, significance = experimental_aggregation.get_needs(needs, infrastructure, social_group, living_situation, service)
    if walking_time_cost == 0 and transport_time_cost == 0 and personal_transport_time_cost == 0 and intensity == 0 and significance == 0:
        print(f'No data found for needs (social_group = {social_group}, living_situation = {living_situation}, city_function = {service})')
        raise Exception(f'No data found for needs (social_group = {social_group}, living_situation = {living_situation}, city_function = {service})')

    if 'walking_time_cost' in kwargs:
        walking_time_cost = int(kwargs['walking_time_cost'])
    if 'transport_time_cost' in kwargs:
        transport_time_cost = int(kwargs['transport_time_cost'])
    if 'personal_transport_time_cost' in kwargs:
        personal_transport_time_cost = int(kwargs['personal_transport_time_cost'])
    if 'intensity' in kwargs:
        intensity = int(kwargs['intensity'])
    if 'significance' in kwargs:
        significance = int(kwargs['significance'])
    walking_availability = float(kwargs.get('walking_availability', 1))
    public_transport_availability_multiplier = float(kwargs.get('public_transport_availability_multiplier', 1))
    personal_transport_availability_multiplier = float(kwargs.get('personal_transport_availability_multiplier', 0))
    max_target_s = float(kwargs.get('max_target_s', 30.0))
    target_s_divider = float(kwargs.get('target_s_divider', 6))
    coeff_multiplier = float(kwargs.get('coeff_multiplier', 5))

    if walking_time_cost == 0 and transport_time_cost == 0 and (personal_transport_time_cost == 0 or personal_transport_availability_multiplier == 0):
        return {
            'walking_geometry': json.dumps({'type': 'Polygon', 'coordinates': []}),
            'transport_geometry': json.dumps({'type': 'Polygon', 'coordinates': []}),
            'car_geometry': json.dumps({'type': 'Polygon', 'coordinates': []}),
            'services': dict(),
            'provision_result': 0.0,
            'parameters': {
                'walking_time_cost': walking_time_cost,
                'transport_time_cost': transport_time_cost,
                'personal_transport_time_cost': personal_transport_time_cost,
                'intensity': intensity,
                'significance': significance
            }
        }

    # Walking

    # walking_geometry = avaliability.get_walking(*coords, walking_time_cost)
    walking_geometry, transport_geometry, car_geometry = avaliability.ensure_ready(*coords, walking_time_cost, transport_time_cost, personal_transport_time_cost)

    df_target_servs = services_buildings[
            services_buildings['service_id'].isin(calculate_services_cnt.count_service(coords, service, walking_time_cost, 'walking', provision_conn, houses_conn))
            &
            (services_buildings['service_type'] == service)
    ]
    df_target_servs = df_target_servs.join(pd.Series(['walking'] * df_target_servs.shape[0], name='availability_type', dtype=str, index=df_target_servs.index))

    # public transport

    # transport_geometry = avaliability.get_transport(*coords, transport_time_cost)

    transport_servs = services_buildings[
            services_buildings['service_id'].isin(calculate_services_cnt.count_service(coords, service, transport_time_cost, 'transport', provision_conn, houses_conn))
            &
            (services_buildings['service_type'] == service)
    ]
    transport_servs = transport_servs.set_index('service_id').drop(df_target_servs['service_id'], errors='ignore').reset_index()
    transport_servs = transport_servs.join(pd.Series(['public_transport'] * transport_servs.shape[0], name='availability_type', dtype=str, index=transport_servs.index))

    df_target_servs = df_target_servs.append(transport_servs, ignore_index=True)
    del transport_servs

    # perosonal_transport (car)

    # car_geometry = avaliability.get_car(*coords, personal_transport_time_cost)
    
    car_servs = services_buildings[
            services_buildings['service_id'].isin(calculate_services_cnt.count_service(coords, service, personal_transport_time_cost, 'car', provision_conn, houses_conn))
            &
            (services_buildings['service_type'] == service)
    ]
    car_servs = car_servs.set_index('service_id').drop(df_target_servs['service_id'], errors='ignore').reset_index()
    car_servs = car_servs.join(pd.Series(['personal_transport'] * car_servs.shape[0], name='availability_type', dtype=str, index=car_servs.index))
    df_target_servs = df_target_servs.append(car_servs, ignore_index=True)
    del car_servs

    # Выполнить расчет атомарной обеспеченности
    # Задать начальное значение обеспеченности
    target_O = 0.0

    # Расчет выполняется при наличии точек оказания услуг на полигоне доступности, иначе обеспеченность - 0
    if not df_target_servs.empty:

        # Рассчитать доступность D услуг из целевого дома для целевой социальной группы
        # Если услуга расположена в пределах требуемой пешей доступности (на полигоне пешей доступности), то D = 1.
        # Если услуга расположена вне пешей доступности, но удовлетворяет требованиям транспортной доступности,
        # то D = 1/I (I - интенсивность использования типа услуги целевой социальной группой).

        # Рассчитать доступность D услуг
        df_target_servs['availability'] = np.where(df_target_servs['availability_type'] == 'walking', walking_availability,
                np.where(df_target_servs['availability_type'] == 'public_transport',
                        round(1 / intensity * public_transport_availability_multiplier, 2), round(1 / intensity * personal_transport_availability_multiplier, 2)))

        # Вычислить мощность S предложения по целевому типу услуги для целевой группы
        target_S = (df_target_servs['power'] * df_target_servs['availability']).sum()

        # Если рассчитанная мощность S > max_target_s, то S принимается равной max_target_s
        if target_S > max_target_s:
            target_S = max_target_s

        # Вычислить значение обеспеченности О
        if significance == 0.5:
            target_O = target_S / target_s_divider
        else:
            coeff = abs(significance - 0.5) * coeff_multiplier
            if significance > 0.5:
                target_O = ((target_S / target_s_divider) ** (coeff + 1)) / (5 ** coeff)
            else:
                target_O = 5 - ((5 - target_S / target_s_divider) ** (coeff + 1)) / (5 ** coeff)

    target_O = round(target_O, 2)

    return {
        'walking_geometry': json.loads(walking_geometry) if walking_geometry is not None else json.dumps({'type': 'Polygon', 'coordinates': []}),
        'transport_geometry': json.loads(transport_geometry) if transport_geometry is not None else json.dumps({'type': 'Polygon', 'coordinates': []}),
        'car_geometry': json.loads(car_geometry) if car_geometry is not None else json.dumps({'type': 'Polygon', 'coordinates': []}),
        'services': list(df_target_servs.transpose().to_dict().values()),
        'provision_result': target_O,
        'parameters': {
            'walking_time_cost': walking_time_cost,
            'transport_time_cost': transport_time_cost,
            'personal_transport_time_cost': personal_transport_time_cost,
            'intensity': intensity,
            'significance': significance
        }
    }


def get_aggregation(where: Union[str, Tuple[float, float]], where_type: str, social_group: Optional[str],
        living_situation: Optional[str], city_function: Optional[str], provision_conn: psycopg2.extensions.connection,
        houses_conn: psycopg2.extensions.connection, update: bool = False) -> Dict[str, Union[float, str]]:
    given_vaules = (social_group, living_situation, city_function)
    found_id: Optional[int] = None
    where_column = 'district' if where_type == 'districts' else 'municipality'
    # city_function: Optional[str] = infrastructure[infrastructure['service'] == service]['function'].iloc[0] if service is not None else None

    with provision_conn.cursor() as cur_provision:
        if where_type in ('districts', 'municipalities'):
            cur_provision.execute(f'SELECT id, avg_intensity, avg_significance, avg_provision, time_done FROM aggregation_{where_column}'
                    ' WHERE social_group_id ' + ('= (SELECT id from social_groups where name = %s)' if social_group is not None else 'is %s') +
                    ' AND living_situation_id ' + ('= (SELECT id from living_situations where name = %s)' if living_situation is not None else 'is %s') +
                    ' AND city_function_id ' + ('= (SELECT id from city_functions where name = %s)' if city_function is not None else 'is %s') +
                    f' AND {where_column}_id = (SELECT id from {where_type} where full_name = %s)',
                    (social_group, living_situation, city_function, where))
            cur_data = cur_provision.fetchall()
            if len(cur_data) != 0:
                id, intensity, significance, provision, done =  cur_data[0]
                if not update:
                    return {
                        'provision': provision,
                        'intensity': intensity,
                        'significance': significance,
                        'time_done': done
                    }
                else:
                    found_id = id
        elif where_type == 'house':
            cur_provision.execute('SELECT id, avg_intensity, avg_significance, avg_provision, time_done FROM aggregation_house'
                    ' WHERE social_group_id ' + ('= (SELECT id from social_groups where name = %s)' if social_group is not None else 'is %s') +
                    ' AND living_situation_id ' + ('= (SELECT id from living_situations where name = %s)' if living_situation is not None else 'is %s') +
                    ' AND city_function_id ' + ('= (SELECT id from city_functions where name = %s)' if city_function is not None else 'is %s') +
                    ' AND latitude = %s AND longitude = %s',
                    (social_group, living_situation, city_function, *where))
            cur_data = cur_provision.fetchall()
            if len(cur_data) != 0:
                id, intensity, significance, provision, done = cur_data[0]
                if not update:
                    return {
                        'provision': provision,
                        'intensity': intensity,
                        'significance': significance,
                        'time_done': done
                    }
                else:
                    found_id = id
        elif where_type == 'total':
            raise Exception('This method is not available for now')
        else:
            raise Exception(f'Unknown aggregation type: "{where_type}"')

    del cur_data

    if social_group is None:
        soc_groups = get_social_groups(city_function, living_situation, to_list=True)
    else:
        soc_groups = [social_group]
    
    if living_situation is None:
        situations = get_living_situations(social_group, city_function, to_list=True)
    else:
        situations = [living_situation]

    functions: List[str]
    if city_function is None:
        functions = get_city_functions(social_group, living_situation, to_list=True)
    else:
        functions = [city_function]

    houses: List[Tuple[float, float]]
    if where_type in ('municipalities', 'districts'):
        houses = list(map(lambda x: (x[1]['latitude'], x[1]['longitude']), all_houses[all_houses[where_column] == where].iterrows()))
    else:
        houses = [where] # type: ignore

    cnt_houses = 0
    provision_houses = 0.0
    intensity_houses = 0.0
    significance_houses = 0.0
    
    for house in houses:
        cnt_groups = 0
        provision_group = 0.0
        intensity_group = 0.0
        significance_group = 0.0
        groups_provision = dict()
        for social_group in soc_groups:
            social_group_needs = needs[needs['social_group'] == social_group]
            cnt_functions = 0
            provision_function = 0.0
            intensity_function = 0.0
            significance_function = 0.0
            for city_function in functions:
                city_function_needs = social_group_needs[social_group_needs['city_function'] == city_function]
                if city_function_needs.shape[0] == 0 or (len(soc_groups) != 1 and len(functions) != 1 and city_function_needs['significance'].max() <= 0.5):
                    continue
                cnt_atomic = 0
                provision_atomic = 0.0
                intensity_atomic = 0.0
                significance_atomic = 0.0
                for living_situation in situations:
                    living_situation_needs = city_function_needs[city_function_needs['living_situation'] == living_situation]
                    if living_situation_needs.shape[0] == 0 or living_situation_needs.iloc[0]['walking'] == 0 and living_situation_needs.iloc[0]['transport'] == 0:
                        continue
                    try:
                        prov = compute_atomic_provision(social_group, living_situation, city_function, house, provision_conn, houses_conn)
                        provision_atomic += prov['provision_result']
                        intensity_atomic += prov['parameters']['intensity']
                        significance_atomic += prov['parameters']['significance']
                        cnt_atomic += 1
                    except Exception as ex:
                        print(f'Exception occured: {ex}')
                        traceback.print_exc()
                        pass
                if cnt_atomic != 0:
                    provision_function += provision_atomic / cnt_atomic
                    intensity_function += intensity_atomic / cnt_atomic
                    significance_function += significance_atomic / cnt_atomic
                    cnt_functions += 1
            if cnt_functions != 0:
                provision_group += provision_function / cnt_functions
                intensity_group += intensity_function / cnt_functions
                significance_group += intensity_function / cnt_functions
                groups_provision[social_group] = (provision_function / cnt_functions, intensity_function / cnt_functions, intensity_function / cnt_functions)
                cnt_groups += 1

        with houses_conn.cursor() as cur_houses:
            cur_houses.execute('SELECT sum(ss.number) FROM social_structure ss'
                    ' INNER JOIN social_groups sg on ss.social_group_id = sg.id'
                    ' WHERE house_id in (SELECT id from houses WHERE ROUND(ST_X(ST_Centroid(geometry))::numeric, 3)::float = %s'
                    ' AND ROUND(ST_Y(ST_Centroid(geometry))::numeric, 3)::float = %s)', (house[0], house[1]))
            res = cur_houses.fetchall()
            if len(soc_groups) != 1:
                if len(res) != 0 and res[0][0] is not None:
                    cnt_functions = res[0][0]
                    provision_group = 0
                    intensity_group = 0
                    significance_group = 0
                    for social_group in groups_provision.keys():
                        cur_houses.execute('SELECT ss.number FROM social_structure ss'
                                ' INNER JOIN social_groups sg on ss.social_group_id = sg.id'
                                ' WHERE house_id in (SELECT id FROM houses WHERE ROUND(ST_X(ST_Centroid(geometry))::numeric, 3)::float = %s'
                                ' AND ROUND(ST_Y(ST_Centroid(geometry))::numeric, 3)::float = %s) AND sg.name = %s union select 0', (house[0], house[1], social_group))
                        number = cur_houses.fetchall()[0][0]
                        provision_group += groups_provision[social_group][0] * number / cnt_functions
                        intensity_group += groups_provision[social_group][1] * number / cnt_functions
                        significance_group += groups_provision[social_group][2] * number / cnt_functions
                elif len(groups_provision) != 0:
                    provision_group = sum(map(lambda x: x[0], groups_provision.values())) / len(groups_provision)
                    intensity_group = sum(map(lambda x: x[1], groups_provision.values())) / len(groups_provision)
                    significance_group = sum(map(lambda x: x[2], groups_provision.values())) / len(groups_provision)
                else:
                    provision_group = 0
                    intensity_group = 0
                    significance_group = 0
            elif cnt_groups != 0:
                provision_group /= cnt_groups
                intensity_group /= cnt_groups
                significance_group /= cnt_groups
        provision_houses += provision_group
        intensity_houses += intensity_group
        significance_houses += intensity_group
        cnt_houses += 1
                
    if cnt_houses != 0:
        provision_houses = round(provision_houses / cnt_houses, 2)
        intensity_houses = round(intensity_houses / cnt_houses, 2)
        significance_houses = round(significance_houses / cnt_houses)
    done_time: Any = time.localtime()
    done_time = f'{done_time.tm_year}-{done_time.tm_mon}-{done_time.tm_mday} {done_time.tm_hour}:{done_time.tm_min}:{done_time.tm_sec}'

    try:
        with provision_conn.cursor() as cur_provision:
            if where_type in ('districts', 'municipalities'):
                if found_id is None:
                    cur_provision.execute(f'INSERT INTO aggregation_{where_column} (social_group_id, living_situation_id, city_function_id, {where_column}_id, avg_intensity, avg_significance, avg_provision, time_done)'
                            ' VALUES ((SELECT id from social_groups where name = %s), (SELECT id from living_situations where name = %s), (SELECT id from city_functions where name = %s),'
                            f' (SELECT id from {where_type} where full_name = %s), %s, %s, %s, %s)',
                            (*given_vaules, where, intensity_houses, significance_houses, provision_houses, done_time))
                else:
                    cur_provision.execute(f'UPDATE aggregation_{where_column} SET avg_intensity = %s, avg_significance = %s, avg_provision = %s, time_done = %s WHERE id = %s',
                            (intensity_houses, significance_houses, provision_houses, done_time, found_id))
            else:
                if found_id is None:
                    cur_provision.execute('INSERT INTO aggregation_house (social_group_id, living_situation_id, city_function_id, latitude, longitude, avg_intensity, avg_significance, avg_provision, time_done)'
                            'VALUES ((SELECT id from social_groups where name = %s), (SELECT id from living_situations where name = %s), (SELECT id from city_functions where name = %s),'
                            ' %s, %s, %s, %s, %s, %s)',
                            (*given_vaules, *where, intensity_houses, significance_houses, provision_houses, done_time))
                else:
                    cur_provision.execute('UPDATE aggregation_house SET intensity = %s, avg_significance = %s, avg_provision = %s, time_done = %s WHERE id = %s', 
                            (intensity_houses, significance_houses, provision_houses, done_time, found_id))
            provision_conn.commit()
    except Exception:
        provision_conn.rollback()
        raise
    return {
        'provision': provision_houses,
        'intensity': intensity_houses,
        'significance': significance_houses,
        'time_done': done_time
    }

def aggregate_district(district: str, social_group: str, living_situation: str, city_function: str,
        provision_conn: psycopg2.extensions.connection, houses_conn:psycopg2.extensions.connection) -> None:
    print(f'Aggregating social_group({social_group}) + living_situation({living_situation}) + city_function({city_function}) + district({district})')
    start = time.time()
    res = get_aggregation(district, 'districts', social_group, living_situation, city_function, provision_conn, houses_conn, False)
    print(f'Finished    social_group({social_group}) + living_situation({living_situation}) + city_function({city_function}) + district({district})'
            f' in {time.time() - start:6.2f} seconds (total_value = {res["provision"]:.2f})')

def aggregate_municipality(municipality: str, social_group: str, living_situation: str, city_function: str,
        provision_conn: psycopg2.extensions.connection, houses_conn:psycopg2.extensions.connection) -> None:
    print(f'Aggregating social_group({social_group}) + living_situation({living_situation}) + city_function({city_function}) + municipality({municipality})')
    start = time.time()
    res = get_aggregation(municipality, 'municipalities', social_group, living_situation, city_function, provision_conn, houses_conn, False)
    print(f'Finished    social_group({social_group}) + living_situation({living_situation}) + city_function({city_function}) + municipality({municipality})'
            f' in {time.time() - start:6.2f} seconds (total_value = {res["provision"]:.2f})')

def update_aggregation(district_or_municipality: str, including_municipalities: bool = False) -> None:
    full_start = time.time()
    if district_or_municipality in city_hierarchy['district_full_name'].unique():
        district = True
    else:
        district = False
    tp = ThreadPool(8, [lambda: ('houses_conn', psycopg2.connect(properties.houses_conn_string)),
            lambda: ('provision_conn', psycopg2.connect(properties.provision_conn_string))], max_size=10)
    try:
        for social_group in get_social_groups(to_list=True) + [None]: # type: ignore
            for city_function in get_city_functions(social_group, to_list=True) + [None]: # type: ignore
                for living_situation in get_living_situations(social_group, city_function, to_list=True) + [None]: # type: ignore
                    if district:
                        try:
                            tp.execute(aggregate_district, (district_or_municipality, social_group, living_situation, city_function))
                        except Exception as ex:
                            traceback.print_exc()
                            print(f'Exception occured! {ex}')
                        if including_municipalities:
                            for municipality in all_houses[all_houses['district'] == district]['municipality'].unique():
                                try:
                                    tp.execute(aggregate_municipality, (municipality, social_group, living_situation, city_function))
                                except Exception as ex:
                                    traceback.print_exc()
                                    print(f'Exception occured! {ex}')
                    else:
                        try:
                            tp.execute(aggregate_municipality, (district_or_municipality, social_group, living_situation, city_function))
                        except Exception as ex:
                            traceback.print_exc()
                            print(f'Exception occured! {ex}')

    except KeyboardInterrupt:
        print('Interrupted by user')
        tp.stop()
        tp.join()
    finally:
        print(f'Finished updating all agregations in {time.time() - full_start:.2f} seconds')

def update_global_data() -> None:
    global all_houses
    global needs
    global infrastructure
    global services_buildings
    global blocks
    global city_hierarchy
    with properties.houses_conn.cursor() as cur:
        cur.execute('SELECT DISTINCT dist.full_name, muni.full_name, ROUND(ST_X(ST_Centroid(h.geometry))::numeric, 3)::float as latitude, ROUND(ST_Y(ST_Centroid(h.geometry))::numeric, 3)::float as longitude FROM houses h inner join districts dist on dist.id = h.district_id inner join municipalities muni on muni.id = h.municipal_id')
        all_houses = pd.DataFrame(cur.fetchall(), columns=('district', 'municipality', 'latitude', 'longitude'))

        cur.execute('SELECT i.name, f.name, s.name from city_functions f JOIN infrastructure_types i ON i.id = f.infrastructure_type_id'
                ' JOIN service_types s ON s.city_function_id = f.id ORDER BY i.name,f.name,s.name;')
        infrastructure = pd.DataFrame(cur.fetchall(), columns=('infrastructure', 'function', 'service'))

        cur.execute('SELECT s.name, l.name, f.name, n.walking, n.public_transport, n.personal_transport, n.intensity FROM needs n'
                ' JOIN social_groups s ON s.id = n.social_group_id'
                ' JOIN living_situations l ON l.id = n.living_situation_id'
                ' JOIN city_functions f ON f.id = n.city_function_id'
                ' ORDER BY s.name, l.name, f.name')
        needs = pd.DataFrame(cur.fetchall(), columns=('social_group', 'living_situation', 'city_function', 'walking', 'transport', 'car', 'intensity'))
        cur.execute('SELECT s.name, f.name, v.significance FROM values v'
                ' JOIN social_groups s ON s.id = v.social_group_id'
                ' JOIN city_functions f ON f.id = v.city_function_id')
        tmp = pd.DataFrame(cur.fetchall(), columns=('social_group', 'city_function', 'significance'))
        needs = needs.merge(tmp, on=['social_group', 'city_function'], how='inner')

        cur.execute('SELECT p.id, b.address, f.name, ST_AsGeoJSON(ST_Centroid(p.geometry)), f.capacity, st.name FROM buildings b'
                ' JOIN physical_objects p ON b.physical_object_id = p.id'
                ' JOIN phys_objs_fun_objs pf ON p.id = pf.phys_obj_id'
                ' JOIN functional_objects f ON f.id = pf.fun_obj_id'
                ' JOIN service_types st on f.service_type_id = st.id'
                ' ORDER BY p.id')
        services_buildings = pd.DataFrame(cur.fetchall(), columns=('service_id', 'address', 'service_name', 'location', 'power', 'service_type'))
        services_buildings['location'] = pd.Series(
            map(lambda geojson: (round(float(geojson[geojson.find('[') + 1:geojson.rfind(',')]), 4), round(float(geojson[geojson.rfind(',') + 1:-2]), 4)),
                services_buildings['location'])
        )
        cur.execute('SELECT m.id, m.full_name, m.short_name, m.population, d.id, d.full_name, d.short_name, d.population FROM municipalities m'
                ' JOIN districts d on d.id = m.district_id ORDER BY d.full_name, m.full_name')
        city_hierarchy = pd.DataFrame(cur.fetchall(), columns=('municipality_id', 'municipality_full_name', 'municipality_short_name',
                'municipality_population', 'district_id', 'district_full_name', 'district_short_name', 'district_population'))
    with properties.houses_conn.cursor() as cur:
        cur.execute('SELECT b.id, b.population, m.full_name as municipality, d.full_name as district FROM'
            ' blocks b JOIN municipalities m ON m.id = b.municipality_id JOIN districts d ON d.id = m.district_id ORDER BY 4, 3, 1')
        blocks = pd.DataFrame(cur.fetchall(), columns=('id', 'population', 'municipality', 'district')).set_index('id')
    # blocks['population'] = blocks['population'].fillna(-1).astype(int)
    blocks['population'] = blocks['population'].replace({np.nan: None})
    with properties.provision_conn.cursor() as cur:
        cur.execute('SELECT s.block_id, ss.social_groups, s.services FROM blocks_soc_groups ss JOIN blocks_services s on s.block_id = ss.block_id')
        blocks = blocks.join(pd.DataFrame(cur.fetchall(), columns=('id', 'social_groups', 'services')).set_index('id'))


compress = Compress()

app = Flask(__name__)
compress.init_app(app)

@app.after_request
def after_request(response) -> Response:
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = '*'
    response.headers['Access-Control-Allow-Headers'] = '*'
    return response

@app.route('/api/reload_data/', methods=['POST'])
def reload_data() -> Response:
    update_global_data()
    return make_response('OK')

# Расчет обеспеченности для атомарной ситуации: обеспеченность одной социальной группы в одной жизненной ситуации
# одной городской функцией, относительно одного жилого дома.

# Для сервисов передаются следующие атрибуты:
# -  идентификатор (service_id)
# -  название сервиса(service_name)
# -  признак принадлежности изохрону пешеходной доступности (walking_dist, boolean)
# -  признак принадлежности изохронам транспортной доступности (transport_dist, boolean)
# -  мощность сервиса (power, со значениями от 1 до 10)

# Сервис возвращает числовую оценку обеспеченности целевой социальной группы в целевой жизненной ситуации сервисами,
# относящимися к целевой городской функции, в целевой точке (доме)
@app.route('/api/provision/atomic', methods=['GET'])
@app.route('/api/provision/atomic/', methods=['GET'])
def atomic_provision() -> Response:
    if not ('social_group' in request.args and 'living_situation' in request.args and 'service' in request.args and 'location' in request.args):
        return make_response(jsonify({'error': 'Request must include all of the ("social_group", "living_situation", "service", "location") arguments'}), 400)
    social_group: str = request.args['social_group'] # type: ignore
    living_situation: str = request.args['living_situation'] # type: ignore
    service: str = request.args['service'] # type: ignore
    if not (social_group in get_social_groups(to_list=True) and living_situation in get_living_situations(to_list=True) \
                and service in get_services(to_list=True)):
        return make_response(jsonify({'error': f"At least one of the ('social_group', 'living_situation', 'city_function') is not in the list of avaliable"
                f' ({social_group in get_social_groups(to_list=True)}, {living_situation in get_living_situations(to_list=True)},'
                        f' {service in get_services(to_list=True)})'}), 400)
    coords: Tuple[int, int] = tuple(map(float, request.args['location'].split(','))) # type: ignore

    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': compute_atomic_provision(coords=coords, houses_conn=properties.houses_conn, provision_conn=properties.provision_conn, **request.args)
    }))

@app.route('/api/provision/aggregated', methods=['GET'])
@app.route('/api/provision/aggregated/', methods=['GET'])
def aggregated_provision() -> Response:
    social_group: Optional[str] = request.args.get('social_group')
    living_situation: Optional[str] = request.args.get('living_situation')
    city_function: Optional[str] = request.args.get('city_function')
    location: Optional[str] = request.args.get('location')
    if not ((social_group is None or social_group == 'all' or social_group in get_social_groups(to_list=True))
            and (living_situation is None or living_situation == 'all' or living_situation in get_living_situations(to_list=True))
            and (city_function is None or city_function == 'all' or city_function in get_city_functions(to_list=True))):
        return make_response(jsonify({'error': "At least one of the ('social_group', 'living_situation', 'city_function') is not in the list of avaliable"}), 400)
    launch_aggregation = True if 'launch_aggregation' in request.args else False
    
    soc_groups: Union[List[str], List[Optional[str]]] = social_groups if social_group == 'all' else [social_group] # type: ignore
    situations: Union[List[str], List[Optional[str]]] = living_situations if living_situation == 'all' else [living_situation] # type: ignore
    functions: Union[List[str], List[Optional[str]]] = city_functions if city_function == 'all' else [city_function] # type: ignore
    where: List[Union[str, Tuple[float, float]]]
    where_type: str
    if location is None:
        where = ['total']
        where_type = 'total'
        raise Exception("Getting total aggregaation is unsupported at the moment. You need to set 'location' parameter.")
    elif location.startswith('inside_'): # type: ignore
        name = location[7:] # type: ignore
        if name not in city_hierarchy['district_full_name'].unique():
            return make_response(jsonify({'error': f"'{name}'' should be a district, but it is not in the list"}), 400)
        where_type = 'municipalities'
        where = list(all_houses[all_houses['district'] == name]['municipality'].unique())
    elif location in city_hierarchy['district_full_name'].unique():
        where = [location]
        where_type = 'districts'
    elif location in city_hierarchy['municipality_full_name'].unique():
        where = [location]
        where_type = 'municipalities'
    else:
        try:
            where = [tuple(map(float, request.args.get('location', type=str).split(',')))] # type: ignore
            where_type = 'house'
        except:
            return make_response(jsonify({'error': f"Cannot find '{location}' in any of the 'districts' or 'municipalities', or parse as a house coordinates"}), 400)

    where_column = 'district' if where_type == 'districts' else 'house' if where_type == 'house' else 'municipality'
    res: List[Dict[str, Dict[str, Any]]] = list()
    with properties.provision_conn.cursor() as cur:
        for now_where in where:
            for now_soc_group in soc_groups:
                for now_situation in situations:
                    cur.execute(f'SELECT DISTINCT f.name FROM aggregation_{where_column} a' + 
                            (f' LEFT JOIN {where_type} w ON w.id = a.{where_column}_id' if where_type != 'house' else '') +
                            ' LEFT JOIN social_groups s ON s.id = a.social_group_id'
                            ' LEFT JOIN living_situations l ON l.id = a.living_situation_id'
                            ' LEFT JOIN city_functions f ON f.id = a.city_function_id' +
                            (' WHERE w.full_name = %s' if where_type != 'house' else ' WHERE a.latitude = %s AND a.longitude = %s') +
                            ' AND s.name = %s AND l.name = %s',
                            (now_where, now_soc_group, now_situation) if where_type != 'house' else (now_where[0], now_where[1], now_soc_group, now_situation))
                    ready_functions = list(map(lambda x: x[0], cur.fetchall()))
                    for now_function in functions:
                        if now_function in ready_functions or launch_aggregation:
                            if now_function not in ready_functions:
                                print(f'location({location}) + social_group({now_soc_group}) + living_situation({now_situation}) + city_function({now_function}) is missing, aggregating')
                            res.append({
                                'params': {
                                    'location': now_where,
                                    'social_group': now_soc_group,
                                    'living_situation': now_situation,
                                    'city_function': now_function,
                                },
                                'result': get_aggregation(now_where, where_type, now_soc_group, now_situation, now_function, properties.provision_conn, properties.houses_conn)
                            })
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': social_group,
                'living_situation': living_situation,
                'city_function': city_function,
                'location': location,
                'where_type': where_type,
                'launch_aggregation': launch_aggregation
            },
            'provision': res
        }
    }))
   
@app.route('/api/provision/alternative', methods=['GET'])
@app.route('/api/provision/alternative/', methods=['GET'])
def alternative_aggregated_provision():
    social_group: Optional[str] = request.args.get('social_group')
    living_situation: Optional[str] = request.args.get('living_situation')
    service: Optional[str] = request.args.get('service')
    location: Optional[str] = request.args.get('location')
    if not ((social_group is None or social_group == 'all' or social_group in get_social_groups(to_list=True))
            and (living_situation is None or living_situation == 'all' or living_situation in get_living_situations(to_list=True))
            and (service is None or service == 'all' or service in infrastructure['service'].unique())):
        return make_response(jsonify({'error': "At least one of the ('social_group', 'living_situation', 'city_function') is not in the list of avaliable"}), 400)
    
    soc_groups: Union[List[str], List[Optional[str]]] = get_social_groups(to_list=True) if social_group == 'all' else [social_group] # type: ignore
    situations: Union[List[str], List[Optional[str]]] = get_living_situations(to_list=True) if living_situation == 'all' else [living_situation] # type: ignore
    services: Union[List[str], List[Optional[str]]] = get_services(to_list=True) if service == 'all' else [service] # type: ignore
    where: List[Union[int, str, Tuple[float, float]]]
    if location is None:
        where = ['city']
        raise NotImplementedError("Getting city aggregation is unsupported at the moment. You need to set 'location' parameter.")
    elif location.startswith('inside_'): # type: ignore
        name = location[7:] # type: ignore
        if name in city_hierarchy['district_full_name'].unique():
            where = list(all_houses[all_houses['district'] == name]['municipality'].unique())
        elif name in city_hierarchy['municipality_full_name'].unique():
            where = list(blocks[blocks['municipality'] == name].index)
        else:
            return make_response(jsonify({'error': f"'{name}' should be a district, but it is not in the list"}), 400)
    elif location in city_hierarchy['district_full_name'].unique() or location in city_hierarchy['municipality_full_name'].unique():
        where = [location]
    else:
        try:
            where = [tuple(map(float, request.args.get('location', type=str).split(',')))] # type: ignore
            return make_response(jsonify({'error': 'Alternative aggregation method does not support aggregation on house'}))
        except:
            return make_response(jsonify({'error': f"Cannot find '{location}' in any of the 'districts' or 'municipalities'"}), 400)

    res: List[Dict[str, Dict[str, Any]]] = list()
    for now_where in where:
        for now_soc_group in soc_groups:
            for now_situation in situations:
                for now_service in services:
                    res.append({
                        'params': {
                            'location': now_where,
                            'social_group': now_soc_group,
                            'living_situation': now_situation,
                            'service': now_service,
                        },
                        'result': experimental_aggregation.aggregate(needs, infrastructure, blocks, now_where,
                                now_soc_group, now_situation, now_service, 'return_debug_info' in request.args)
                    })
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': social_group,
                'living_situation': living_situation,
                'service': service,
                'location': location,
            },
            'provision': res
        }
    }))

@app.route('/api/provision/ready/houses', methods=['GET'])
@app.route('/api/provision/ready/houses/', methods=['GET'])
def ready_houses() -> Response:
    social_group: Optional[str] = request.args.get('social_group')
    living_situation: Optional[str] = request.args.get('living_situation')
    city_function: Optional[str] = request.args.get('city_function')
    house: Tuple[Optional[float], Optional[float]]
    if 'house' in request.args:
        house = tuple(map(float, request.args['house'].split(','))) # type: ignore
    else:
        house = (None, None)
    with properties.provision_conn.cursor() as cur:
        cur_str = 'SELECT soc.name, liv.name, fun.name, a.latitude, a.longitude, avg_provision' \
            ' FROM aggregation_house a JOIN living_situations liv ON liv.id = a.living_situation_id' \
            ' JOIN social_groups soc ON soc.id = a.social_group_id' \
            ' JOIN city_functions fun ON fun.id = a.city_function_id'
        wheres = []
        for column_name, value in (('soc.name', social_group), ('liv.name', living_situation), ('fun.name', city_function), ('latitude', house[0]), ('longitude', house[1])):
            if value is not None:
                wheres.append(f"{column_name} = '{value}'")
        if len(wheres) != 0:
            cur_str += ' WHERE ' + ' AND '.join(wheres)
        cur.execute(cur_str)
        ans = pd.DataFrame(cur.fetchall(), columns=('social_group', 'living_situation', 'city_function', 'latitude', 'longitude', 'provision'))
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': social_group,
                'living_situation': living_situation,
                'city_function': city_function,
                'house': f'{house[0]},{house[1]}' if house[0] is not None else None
            },
            'result': list((row[1].to_dict() for row in ans.iterrows()))
        }
    }))

@app.route('/api/provision/ready/districts', methods=['GET'])
@app.route('/api/provision/ready/districts/', methods=['GET'])
def ready_districts() -> Response:
    social_group: Optional[str] = request.args.get('social_group')
    living_situation: Optional[str] = request.args.get('living_situation')
    city_function: Optional[str] = request.args.get('city_function')
    district: Optional[str] = request.args.get('district', None, type=str)
    with properties.provision_conn.cursor() as cur:
        cur_str = 'SELECT soc.name, liv.name, fun.name, dist.full_name, avg_provision' \
            ' FROM aggregation_district a JOIN living_situations liv ON liv.id = a.living_situation_id' \
            ' JOIN social_groups soc ON soc.id = a.social_group_id' \
            ' JOIN city_functions fun ON fun.id = a.city_function_id' \
            ' JOIN districts dist ON dist.id = a.district_id'
        wheres = []
        for column_name, value in (('soc.name', social_group), ('liv.name', living_situation), ('fun.name', city_function), ('dist.full_name', district)):
            if value is not None:
                wheres.append(f"{column_name} = '{value}'")
        if len(wheres) != 0:
            cur_str += ' WHERE ' + ' AND '.join(wheres)
        cur.execute(cur_str)
        ans = pd.DataFrame(cur.fetchall(), columns=('social_group', 'living_situation', 'city_function', 'district', 'provision'))
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': social_group,
                'living_situation': living_situation,
                'city_function': city_function,
                'district': district
            },
            'result': list((row[1].to_dict() for row in ans.iterrows()))
        }
    }))

@app.route('/api/provision/ready/municipalities', methods=['GET'])
@app.route('/api/provision/ready/municipalities/', methods=['GET'])
def ready_municipalities() -> Response:
    social_group: Optional[str] = request.args.get('social_group')
    living_situation: Optional[str] = request.args.get('living_situation')
    city_function: Optional[str] = request.args.get('city_function')
    municipality: Optional[str] = request.args.get('municipality', None, type=str)
    with properties.provision_conn.cursor() as cur:
        cur_str = 'SELECT soc.name, liv.name, fun.name, muni.full_name, avg_provision' \
            ' FROM aggregation_municipality a JOIN living_situations liv ON liv.id = a.living_situation_id' \
            ' JOIN social_groups soc ON soc.id = a.social_group_id' \
            ' JOIN city_functions fun ON fun.id = a.city_function_id' \
            ' JOIN municipalities muni ON muni.id = a.municipality_id'
        wheres = []
        for column_name, value in (('soc.name', social_group), ('liv.name', living_situation), ('fun.name', city_function), ('muni.full_name', municipality)):
            if value is not None:
                wheres.append(f"{column_name} = '{value}'")
        if len(wheres) != 0:
            cur_str += ' WHERE ' + ' AND '.join(wheres)
        cur.execute(cur_str)
        ans = pd.DataFrame(cur.fetchall(), columns=('social_group', 'living_situation', 'city_function', 'municipality', 'provision'))
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': social_group,
                'living_situation': living_situation,
                'city_function': city_function,
                'municipality': municipality
            },
            'result': list((row[1].to_dict() for row in ans.iterrows()))
        }
    }))
    
@app.route('/api/houses', methods=['GET'])
@app.route('/api/houses/', methods=['GET'])
def houses_in_square() -> Response:
    if 'firstPoint' not in request.args or 'secondPoint' not in request.args:
        return make_response(jsonify({'error': "'firstPoint' and 'secondPoint' must be provided as query parameters"}), 400)
    point_1: Tuple[int, int] = tuple(map(float, request.args['firstPoint'].split(','))) # type: ignore
    point_2: Tuple[int, int] = tuple(map(float, request.args['secondPoint'].split(','))) # type: ignore
    with properties.houses_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ROUND(ST_X(ST_Centroid(geometry))::numeric, 3)::float, ROUND(ST_Y(ST_Centroid(geometry))::numeric, 3)::float FROM houses"
                " WHERE ST_WITHIN(geometry, ST_POLYGON(text('LINESTRING({lat1} {lan1}, {lat1} {lan2}, {lat2} {lan2}, {lat2} {lan1}, {lat1} {lan1})'), 4326))".format(
            lat1=point_1[0], lan1 = point_1[1], lat2 = point_2[0], lan2 = point_2[1]
        ))
        return make_response(jsonify({
            '_links': {'self': {'href': request.path}},
            '_embedded': {
                'params': {
                    'firstCoord': f'{point_1[0]},{point_1[1]}',
                    'secondCoord': f'{point_2[0]},{point_2[1]}'
                },
                'houses': list(cur.fetchall())
            }
        }
        ))

def get_social_groups(city_function: Optional[str] = None, living_situation: Optional[str] = None, to_list: bool = False) -> Union[List[str], pd.DataFrame]:
    res = needs[(needs['significance'] > 0) & (needs['intensity'] > 0)]
    if city_function is None:
        res = res.drop(['city_function', 'significance'], axis=True)
    else:
        res = res[res['city_function'] == city_function].drop('city_function', axis=True)
    if living_situation is None:
        res = res.drop(['living_situation', 'intensity'], axis=True)
    else:
        res = res[res['living_situation'] == living_situation].drop('living_situation', axis=True)
    if to_list:
        return list(res['social_group'].unique())
    else:
        return res

@app.route('/api/relevance/social_groups', methods=['GET'])
@app.route('/api/relevance/social_groups/', methods=['GET'])
def relevant_social_groups() -> Response:
    res = get_social_groups(request.args.get('city_function'), request.args.get('living_situation')) # type: pd.DataFrame
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'city_function': request.args.get('city_function'),
                'living_situation': request.args.get('living_situation')
            },
            'social_groups': list(res.drop_duplicates().replace({np.nan: None}).transpose().to_dict().values())
        }
    }))

@app.route('/api/list/social_groups', methods=['GET'])
@app.route('/api/list/social_groups/', methods=['GET'])
def list_social_groups() -> Response:
    res = get_social_groups(request.args.get('city_function'), request.args.get('living_situation'), to_list=True) # type: List[str]
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'city_function': request.args.get('city_function'),
                'living_situation': request.args.get('living_situation')
            },
            'social_groups': res
        }
    }))


def get_city_functions(social_group: Optional[str] = None, living_situation: Optional[str] = None, to_list: bool = False) -> Union[List[str], pd.DataFrame]:
    res = needs[(needs['significance'] > 0) & (needs['intensity'] > 0) & needs['city_function'].isin(infrastructure['function'].dropna().unique())]
    if social_group is None:
        res = res.drop(['social_group', 'significance'], axis=True)
    else:
        res = res[res['social_group'] == social_group].drop('social_group', axis=True)
    if living_situation is None:
        res = res.drop(['living_situation', 'intensity'], axis=True)
    else:
        res = res[res['living_situation'] == living_situation].drop('living_situation', axis=True)
    if to_list:
        return list(res['city_function'].unique())
    else:
        return res

@app.route('/api/relevance/city_functions', methods=['GET'])
@app.route('/api/relevance/city_functions/', methods=['GET'])
def relevant_city_functions() -> Response:
    res = get_city_functions(request.args.get('social_group'), request.args.get('living_situation')) # type: pd.DataFrame
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': request.args.get('social_group'),
                'living_situation': request.args.get('living_situation')
            },
            'city_functions': list(res.drop_duplicates().replace({np.nan: None}).transpose().to_dict().values())
        }
    }))

@app.route('/api/list/city_functions', methods=['GET'])
@app.route('/api/list/city_functions/', methods=['GET'])
def list_city_functions() -> Response:
    res = get_city_functions(request.args.get('social_group'), request.args.get('living_situation'), to_list=True) # type: List[str]
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': request.args.get('social_group'),
                'living_situation': request.args.get('living_situation')
            },
            'city_functions': res
        }
    }))

def get_services(social_group: Optional[str] = None, living_situation: Optional[str] = None, to_list: bool = False) -> Union[List[str], pd.DataFrame]:
    res = needs[(needs['significance'] > 0) & (needs['intensity'] > 0) & needs['city_function'].isin(infrastructure['function'].dropna().unique())]
    if social_group is None:
        res = res.drop(['social_group', 'significance'], axis=True)
    else:
        res = res[res['social_group'] == social_group].drop('social_group', axis=True)
    if living_situation is None:
        res = res.drop(['living_situation', 'intensity'], axis=True)
    else:
        res = res[res['living_situation'] == living_situation].drop('living_situation', axis=True)
    if to_list:
        return list(infrastructure[infrastructure['function'].isin(res['city_function'].unique())]['service'].unique())
    else:
        return res

@app.route('/api/list/services', methods=['GET'])
@app.route('/api/list/services/', methods=['GET'])
def list_services() -> Response:
    res = get_services(request.args.get('social_group'), request.args.get('living_situation'), to_list=True) # type: List[str]
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': request.args.get('social_group'),
                'living_situation': request.args.get('living_situation')
            },
            'services': res
        }
    }))

def get_living_situations(social_group: Optional[str] = None, city_function: Optional[str] = None, to_list: bool = False) -> Union[List[str], pd.DataFrame]:
    res = needs[(needs['significance'] > 0) & (needs['intensity'] > 0)]
    if social_group is not None and city_function is not None:
        res = res[(res['social_group'] == social_group) & (res['city_function'] == city_function)].drop(['city_function', 'social_group'], axis=True)
    elif social_group is not None:
        res = pd.DataFrame(res[res['social_group'] == social_group]['living_situation'].unique(), columns=('living_situation',))
    elif city_function is not None:
        res = pd.DataFrame(res[res['city_function'] == city_function]['living_situation'].unique(), columns=('living_situation',))
    else:
        res = pd.DataFrame(res['living_situation'].unique(), columns=('living_situation',))
    if to_list:
        return list(res['living_situation'].unique())
    else:
        return res

@app.route('/api/relevance/living_situations', methods=['GET'])
@app.route('/api/relevance/living_situations/', methods=['GET'])
def relevant_living_situations() -> Response:
    res = get_living_situations(request.args.get('social_group'), request.args.get('city_function')) # type: pd.DataFrame
    significance: Optional[int] = None
    if 'significance' in res.columns:
        if res.shape[0] > 0:
            significance = next(iter(res['significance']))
        res = res.drop('significance', axis=True)
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': request.args.get('social_group'),
                'city_function': request.args.get('city_function'),
                'significance': significance
            },
            'living_situations': list(res.drop_duplicates().transpose().to_dict().values())
        }
    }))

@app.route('/api/list/living_situations', methods=['GET'])
@app.route('/api/list/living_situations/', methods=['GET'])
def list_living_situations() -> Response:
    res = get_living_situations(request.args.get('social_group'), request.args.get('city_function'), to_list=True) # type: List[str]
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'params': {
                'social_group': request.args.get('social_group'),
                'city_function': request.args.get('city_function'),
            },
            'living_situations': res
        }
    }))

@app.route('/api/list/infrastructures', methods=['GET'])
@app.route('/api/list/infrastructures/', methods=['GET'])
def list_infrastructures() -> Response:
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'infrastructures': tuple([{
                'name': infra,
                'functions': tuple([{
                    'name': function,
                    'services': tuple([service for service in infrastructure[infrastructure['function'] == function].dropna()['service']])
                } for function in infrastructure[infrastructure['infrastructure'] == infra].dropna()['function'].unique()])
            } for infra in infrastructure['infrastructure'].unique()])
        }
    }))

@app.route('/api/list/districts', methods=['GET'])
@app.route('/api/list/districts/', methods=['GET'])
def list_districts() -> Response:
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'districts': list(city_hierarchy['district_full_name'].unique())
        }
    }))

@app.route('/api/list/municipalities', methods=['GET'])
@app.route('/api/list/municipalities/', methods=['GET'])
def list_municipalities() -> Response:
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'municipalities': list(city_hierarchy['municipality_full_name'])
        }
    }))

@app.route('/api/list/city_hierarchy', methods=['GET'])
@app.route('/api/list/city_hierarchy/', methods=['GET'])
def list_city_hierarchy() -> Response:
    local_hierarchy = city_hierarchy
    if 'location' in request.args:
        if request.args['location'] in city_hierarchy['district_full_name'].unique():
            local_hierarchy = local_hierarchy[local_hierarchy['district_full_name'] == request.args['location']]
        elif request.args['location'] in city_hierarchy['municipality_full_name'].unique():
            local_hierarchy = local_hierarchy[local_hierarchy['municipality_full_name'] == request.args['location']]
        elif request.args['location'].isnumeric():
            local_hierarchy = local_hierarchy[local_hierarchy['municipality_full_name'] == blocks.loc[int(request.args['location'])]['municipality']]
        else:
            return make_response(jsonify({'error': f"location '{request.args['location']}'' is not found in any of districts, municipalities or blocks"}), 400)
    
    districts = [{'id': id, 'full_name': full_name, 'short_name': short_name, 'population': population}
            for _, (id, full_name, short_name, population) in
                    local_hierarchy[['district_id', 'district_full_name', 'district_short_name', 'district_population']].drop_duplicates().iterrows()]
    for district in districts:
        district['municipalities'] = [{'id': id, 'full_name': full_name, 'short_name': short_name, 'population': population}
                for _, (id, full_name, short_name, population) in
                        local_hierarchy[local_hierarchy['district_id'] == district['id']][['municipality_id', 'municipality_full_name',
                                'municipality_short_name', 'municipality_population']].iterrows()]
    if 'include_blocks' in request.args:
        for district in districts:
            for municipality in district['municipalities']:
                municipality['blocks'] = [{'id': id, 'population': population} for id, population in
                        blocks[blocks['municipality'] == municipality['full_name']]['population'].items()]
    return make_response(jsonify({
        '_links': {'self': {'href': request.path}},
        '_embedded': {
            'districts': districts,
            'parameters': {
                'include_blocks': 'include_blocks' in request.args,
                'location': request.args.get('location')
            }
        }
    }))

@app.route('/', methods=['GET'])
@app.route('/api/', methods=['GET'])
def api_help() -> Response:
    return make_response(jsonify({
        'version': '2020-12-12-quickfix',
        '_links': {
            'self': {
                'href': request.path
            },
            'atomic_provision': {
                'href': '/api/provision/atomic/{?social_group,living_situation,city_function,location}',
                'templated': True
            },
            'aggregated-provision': {
                'href': '/api/provision/aggregated/{?social_group,living_situation,city_function,location}',
                'templated': True
            },
            'alternative-aggregated-provision': {
                'href': '/api/provision/alternative/{?social_group,living_situation,service,location,return_debug_info}',
                'templated': True
            },
            'get-houses': {
                'href': '/api/houses/{?firstPoint,secondPoint}',
                'templated': True
            },
            'list-social_groups': {
                'href': '/api/list/social_groups/{?city_function,living_situation}',
                'templated': True
            },
            'list-living_situations': {
                'href': '/api/list/living_situations/{?social_group,city_function}',
                'templated': True
            },
            'list-city_functions': {
                'href': '/api/list/city_functions/{?social_group,living_situation}',
                'templated': True
            },
            'list-services': {
                'href': '/api/list/services/{?social_group,living_situation}',
                'templated': True
            },
            'list-city_hierarchy' :{
                'href': '/api/list/city_hierarchy/{?include_blocks,location}',
                'templated': True
            },
            'relevant-social_groups': {
                'href': '/api/relevance/social_groups/{?city_function,living_situation}',
                'templated': True
            },
            'relevant-living_situations': {
                'href': '/api/relevance/living_situations/{?social_group,city_function}',
                'templated': True
            },
            'relevant-city_functions': {
                'href': '/api/relevance/city_functions/{?social_group,living_situation}',
                'templated': True
            },
            'list-infrastructures': {
                'href': '/api/list/infrastructures/'
            },
            'list-districts': {
                'href': '/api/list/districts/'
            },
            'list-municipalities': {
                'href': '/api/list/municipalities/'
            },
            'ready_aggregations_houses': {
                'href': '/api/provision/ready/houses/{?social_group,living_situation,city_function,house}',
                'templated': True
            },
            'ready_aggregations_districts': {
                'href': '/api/provision/ready/districts/{?social_group,living_situation,city_function,district}',
                'templated': True
            },
            'ready_aggregations_municipalities': {
                'href': '/api/provision/ready/municipalities/{?social_group,living_situation,city_function,municipality}',
                'templated': True
            }
        }
    }))

@app.errorhandler(404)
def not_found(error):
    return make_response(jsonify({'error': 'Not found'}), 404)

@app.errorhandler(Exception)
def any_error(error: Exception):
    properties.houses_conn.rollback()
    properties.provision_conn.rollback()
    print(f'{request.path}?{"&".join(map(lambda x: f"{x[0]}={x[1]}", request.args.items()))}')
    traceback.print_exc()
    return make_response(jsonify({
        'error': str(error),
        'error_type': str(type(error)),
        'path': request.path,
        'params': '&'.join(map(lambda x: f'{x[0]}={x[1]}', request.args.items())),
        'trace': list(itertools.chain(*map(lambda x: x.split('\n'), traceback.format_tb(error.__traceback__))))
    }), 500)


if __name__ == '__main__':

    # Default properties settings

    properties = Properties(
            'localhost', 5432, 'provision', 'postgres', 'postgres', 
            'localhost', 5432, 'citydb', 'postgres', 'postgres',
            8080, 'http://10.32.1.61:8080/api.v2/isochrones'
    )
    aggregate_target = '-'

    # Environment variables

    if 'PROVISION_API_PORT' in os.environ:
        properties.api_port = int(os.environ['PROVISION_API_PORT'])
    if 'PROVISION_DB_ADDR' in os.environ:
        properties.provision_db_addr = os.environ['PROVISION_DB_ADDR']
    if 'PROVISION_DB_NAME' in os.environ:
        properties.provision_db_name = os.environ['PROVISION_DB_NAME']
    if 'PROVISION_DB_PORT' in os.environ:
        properties.provision_db_port = int(os.environ['PROVISION_DB_PORT'])
    if 'PROVISION_DB_USER' in os.environ:
        properties.provision_db_user = os.environ['PROVISION_DB_USER']
    if 'PROVISION_DB_PASS' in os.environ:
        properties.provision_db_pass = os.environ['PROVISION_DB_PASS']
    if 'HOUSES_DB_ADDR' in os.environ:
        properties.houses_db_addr = os.environ['HOUSES_DB_ADDR']
    if 'HOUSES_DB_NAME' in os.environ:
        properties.houses_db_name = os.environ['HOUSES_DB_NAME']
    if 'HOUSES_DB_PORT' in os.environ:
        properties.houses_db_port = int(os.environ['HOUSES_DB_PORT'])
    if 'HOUSES_DB_USER' in os.environ:
        properties.houses_db_user = os.environ['HOUSES_DB_USER']
    if 'HOUSES_DB_PASS' in os.environ:
        properties.houses_db_pass = os.environ['HOUSES_DB_PASS']
    if 'PROVISION_AGGREGATE' in os.environ:
        aggregate_target = os.environ['PROVISION_AGGREGATE']
    if 'TRANSPORT_MODEL_ADDR' in os.environ:
        properties.transport_model_api_endpoint = os.environ['TRANSPORT_MODEL_ADDR']

    # CLI Arguments

    parser = argparse.ArgumentParser(
        description='Starts up the provision API server')
    parser.add_argument('-pH', '--provision_db_addr', action='store', dest='provision_db_addr',
                        help=f'postgres host address [default: {properties.provision_db_addr}]', type=str)
    parser.add_argument('-pP', '--provision_db_port', action='store', dest='provision_db_port',
                        help=f'postgres port number [default: {properties.provision_db_port}]', type=int)
    parser.add_argument('-pd', '--provision_db_name', action='store', dest='provision_db_name',
                        help=f'postgres database name [default: {properties.provision_db_name}]', type=str)
    parser.add_argument('-pU', '--provision_db_user', action='store', dest='provision_db_user',
                        help=f'postgres user name [default: {properties.provision_db_user}]', type=str)
    parser.add_argument('-pW', '--provision_db_pass', action='store', dest='provision_db_pass',
                        help=f'database user password [default: {properties.provision_db_pass}]', type=str)
    parser.add_argument('-hH', '--houses_db_addr', action='store', dest='houses_db_addr',
                        help=f'postgres host address [default: {properties.houses_db_addr}]', type=str)
    parser.add_argument('-hP', '--houses_db_port', action='store', dest='houses_db_port',
                        help=f'postgres port number [default: {properties.houses_db_port}]', type=int)
    parser.add_argument('-hd', '--houses_db_name', action='store', dest='houses_db_name',
                        help=f'postgres database name [default: {properties.houses_db_name}]', type=str)
    parser.add_argument('-hU', '--houses_db_user', action='store', dest='houses_db_user',
                        help=f'postgres user name [default: {properties.houses_db_user}]', type=str)
    parser.add_argument('-hW', '--houses_db_pass', action='store', dest='houses_db_pass',
                        help=f'database user password [default: {properties.houses_db_pass}]', type=str)
    parser.add_argument('-hp', '--port', action='store', dest='api_port',
                        help=f'postgres port number [default: {properties.api_port}]', type=int)
    parser.add_argument('-t', '--aggregate_target', action='store', dest='aggregate_target',
                        help=f'aggregate municipality, district with municipalities or everything [default: {aggregate_target}', type=str)
    parser.add_argument('-T', '--transport_model_api', action='store', dest='transport_model_api_endpoint',
                        help=f'url of transport model api [default: {properties.transport_model_api_endpoint}]', type=str)
    args = parser.parse_args()

    if args.provision_db_addr is not None:
        properties.provision_db_addr = args.provision_db_addr
    if args.provision_db_port is not None:
        properties.provision_db_port = args.provision_db_port
    if args.provision_db_name is not None:
        properties.provision_db_name = args.provision_db_name
    if args.provision_db_user is not None:
        properties.provision_db_user = args.provision_db_user
    if args.provision_db_pass is not None:
        properties.provision_db_pass = args.provision_db_pass
    if args.houses_db_addr is not None:
        properties.houses_db_addr = args.houses_db_addr
    if args.houses_db_port is not None:
        properties.houses_db_port = args.houses_db_port
    if args.houses_db_name is not None:
        properties.houses_db_name = args.houses_db_name
    if args.houses_db_user is not None:
        properties.houses_db_user = args.houses_db_user
    if args.houses_db_pass is not None:
        properties.houses_db_pass = args.houses_db_pass
    if args.api_port is not None:
        properties.api_port = args.api_port
    if args.aggregate_target:
        aggregate_target = args.aggregate_target
    
    print('Getting global data')

    update_global_data()

    avaliability = Avaliability()

    if aggregate_target == '-':
        print('Skipping aggregation')
    else:
        print(f'Starting aggregation in 2 seconds: aggregating {aggregate_target}')
        time.sleep(2)
        update_aggregation(aggregate_target, True)

    print(f'Starting application on 0.0.0.0:{properties.api_port} with houses DB ({properties.houses_db_user}@{properties.houses_db_addr}:{properties.houses_db_port}/{properties.houses_db_name}) and'
        f' provision DB ({properties.provision_db_user}@{properties.provision_db_addr}:{properties.provision_db_port}/{properties.provision_db_name}).')
    print(f'Transport model API endpoint is {properties.transport_model_api_endpoint}')

    app.run(host='0.0.0.0', port=properties.api_port)
