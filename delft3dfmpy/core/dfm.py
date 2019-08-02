import itertools
import logging
import os

import geopandas as gpd
import numpy as np
import pandas as pd
import tqdm

from scipy.spatial import KDTree
from shapely.geometry import LineString, Point, Polygon

import delft3dfmpy.converters.hydamo_to_dflowfm as hydamo_to_dflowfm
from delft3dfmpy.core import geometry
from delft3dfmpy.datamodels.common import ExtendedGeoDataFrame
from delft3dfmpy.datamodels.cstructures import meshgeom, meshgeomdim
from delft3dfmpy.io import dfmreader

from delft3dfmpy.core import checks

roughness_delft3dfm =  {
    "chezy": 1,
    "manning": 4,
    "nikuradse": 5,
    "stricklerks": 6,
    "whitecolebrook": 7,
    "bosbijkerk": 9
}

roughness_gml = {
    1: "chezy",
    2: "manning",
    3: "nikuradse",
    4: "stricklerks",
    5: "whitecolebrook",
    6: "bosbijkerk"
}

logger = logging.getLogger(__name__)

class DFlowFMModel:

    def __init__(self):

        
        self.mdu_parameters = {}

        self.network = Network(self)

        self.structures = Structures(self)
        
        self.crosssections = CrossSections(self)

        self.observation_points = ObservationPoints(self)

        self.external_forcings = ExternalForcings(self)

    def export_network(self, output_dir, overwrite=False):

        # Check if files already exist
        files = ['mesh1d.shp', 'mesh2d.shp', 'links1d2d.shp']
        paths = [os.path.join(output_dir, file) for file in files]
        if not overwrite:
            for path in paths:
                if os.path.exists(path):
                    raise FileExistsError(f'Path "{path}" already exists. Choose another output folder or specify overwrite=True.')

        # Links
        links = self.get_1d2dlinks(as_gdf=True)
        links.crs = {'init': 'epsg:28992'}
        links.to_file(paths[2])

        # Mesh2d
        mesh2d = gpd.GeoDataFrame(geometry=[Polygon(poly) for poly in self.mesh2d.get_faces()], crs='epsg:28992')
        mesh2d.to_file(paths[1])

        # Mesh1d
        mesh1d = gpd.GeoDataFrame(geometry=[LineString(line) for line in self.mesh1d.get_segments()], crs='epsg:28992')
        mesh1d.to_file(paths[0])    
    
class ExternalForcings:

    def __init__(self, dflowfmmodel):
        # Point to relevant attributes from parent
        self.dflowfmmodel = dflowfmmodel
        self.initial_waterlevel_polygons = gpd.GeoDataFrame(columns=['waterlevel', 'geometry'])
        self.initial_waterlevel_xyz = []
        self.missing = None
        self.mdu_parameters = dflowfmmodel.mdu_parameters

        # GeoDataFrame for saving boundary conditions
        self.boundaries = gpd.GeoDataFrame(
            columns=['code', 'bctype', 'time', 'value', 'geometry', 'filetype', 'operand', 'method', 'branchid'])
        
        # Dataframe for saving time series for structure
        self.structures = pd.DataFrame(columns=['id', 'type', 'parameter', 'time', 'value'])

        # Dictionary for saving laterals
        self.laterals = {}

        self.io = dfmreader.ExternalForcingsIO(self)

    def set_initial_waterlevel(self, level, polygon=None, name=None):
        """
        Method to set initial water level. A polygon can be given to
        limit the initial water level to a certain extent. 

        The initial waterlevel is added to the ext file
        """
        # Get name is not given as input
        if name is None:
            name = 'wlevpoly{:04d}'.format(len(self.initial_waterlevel_polygons) + 1)

        # Add to geodataframe
        self.initial_waterlevel_polygons.loc[name] = {'waterlevel': level, 'geometry': polygon}

    def set_missing_waterlevel(self, missing):
        """
        Method to set the missing value for the water level.
        this overwrites the water level at missing value in the mdu file.

        Parameters
        ----------
        missing : float
            Water depth
        """
        self.mdu_parameters['WaterLevIni'] = missing
    
    def set_initial_waterdepth(self, depth):
        """
        Method to set the initial water depth model wide. The water depth is
        set by determining the water level at the locations of the cross sections.

        Parameters
        ----------
        depth : float
            Water depth
        """
        
        crosssections = self.dflowfmmodel.crosssections
        if not any(crosssections.crosssection_loc) or self.dflowfmmodel.network.mesh1d.empty():
            raise ValueError('Cross sections or network are not initialized.')

        # Open inition water depth file
        self.mdu_parameters['WaterLevIniFile'] = 'initialconditions/initial_waterlevel.xyz'

        # Get water depths from cross sections
        bottom = crosssections.get_bottom_levels()
        del self.initial_waterlevel_xyz[:]
        self.initial_waterlevel_xyz.extend([[row.geometry.x, row.geometry.y, row.minz + depth] for row in bottom.itertuples()])

    def add_boundary_condition(self, name, pt, bctype, series, branchid=None):

        assert bctype in ['discharge', 'waterlevel']
        
        if isinstance(pt, tuple):
            pt = Point(*pt)

        # If branch is not given explicitly, find the nearest
        if branchid is None:
            branchid = self.dflowfmmodel.network.branches.distance(pt).idxmin()

        # Find nearest branch for geometry
        branch = self.dflowfmmodel.network.schematised.at[branchid, 'geometry']
        extended_line = geometry.extend_linestring(line=branch, near_pt=pt, length=1.0)

        # Create intersection line for boundary condition
        bcline = LineString(geometry.orthogonal_line(line=extended_line, offset=0.1, width=0.1))

        # Convert time to minutes
        if isinstance(series, pd.Series):
            times = ((series.index - series.index[0]).total_seconds() / 60.).tolist()
            values = series.values.tolist()
        else:
            times = None
            values = series

        # Add boundary condition
        self.boundaries.loc[name] = {
            'code': name,
            'bctype': bctype+'bnd',
            'value': values,
            'time': times,
            'geometry': bcline,
            'filetype': 9,
            'operand': 'O',
            'method': 3,
            'branchid': branchid
        }

        # Check if a 1d2d link should be removed
        self.dflowfmmodel.network.links1d2d.check_boundary_link(self.boundaries.loc[name])
    
    def add_rain_series(self, name, values, times):
        # Add boundary condition
        self.boundaries.loc[name] = {
            'code' : name,
            'bctype' : 'rainfall',
            'filetype' : 1,
            'method' : 1,
            'operand' : 'O',
            'value': values,
            'time': times,
            'geometry': None,
            'branchid': None
        }

    def set_structure_series(self, structure_id, structure_type, parameter, times, values):
        # Get filename
        filename = f"{structure_type}_{structure_id}.tim"

        # Add in structure dataframe
        if structure_type == 'weir':
            weirnames = list(self.dflowfmmodel.structures.weirs.keys())
            if structure_id not in weirnames:
                raise IndexError(f'"{structure_id}" not in index: "{",".join(weirnames)}"')
            self.dflowfmmodel.structures.weirs[structure_id][parameter] = filename
        else:
            raise NotImplementedError('Only implemented for weirs.')

        # Add boundary condition
        self.structures.loc[structure_id] = {
            'id' : structure_id,
            'type' : structure_type,
            'parameter' : parameter,
            'time': times,
            'value': values
        }

class CrossSections:

    def __init__(self, dflowfmmodel):
        
        self.io = dfmreader.CrossSectionsIO(self)
        
        self.crosssection_loc = {}
        self.crosssection_def = {}

        self.dflowfmmodel = dflowfmmodel

        self.default_definition = None
        self.default_definition_shift = 0.0

        self.get_roughnessname = self.dflowfmmodel.network.get_roughness_description

        
    
    def set_default_definition(self, definition, shift=0.0):
        """
        Add default profile
        """
        if definition not in self.crosssection_def.keys():
            raise KeyError(f'Cross section definition "{definition}" not found."')

        self.default_definition = definition
        self.default_definition_shift = shift

    def add_yz_definition(self, yz, roughnesstype, roughnessvalue, name=None):
        """
        Add xyz crosssection

        Parameters
        ----------
        code : str
            Id of cross section
        branch : str
            Name of branch
        offset : float
            Position of cross section along branch. If not given, the position is determined
            from the branches in the network. These should thus be given in this case.
        crds : np.array
            Nx2 array with y, z coordinates
        """

        # get coordinates
        length, z = yz.T
        if name is None:
            name = f'yz_{len(crosssection_def):08d}'
        
        # Get roughnessname
        roughnessname = self.get_roughnessname(roughnesstype, roughnessvalue)

        # Add to dictionary
        self.crosssection_def[name] = {
            'id' : name,
            'type': 'yz',
            'yzcount': len(z),
            'yvalues': list_to_str(length),
            'zvalues': list_to_str(z),
            'sectioncount': 1,
            'roughnessnames': roughnessname,
            'roughnesspositions': list_to_str([length[0], length[-1]])
        }

        return name

    def add_circle_definition(self, diameter, roughnesstype, roughnessvalue, name=None):
        """
        Add circle cross section. The cross section name is derived from the shape and roughness,
        so similar cross sections will result in a single definition.
        """        
        # Get name if not given
        if name is None:
            name = f'circ_d{diameter:.3f}'
        
        # Get roughnessname
        roughnessname = self.get_roughnessname(roughnesstype, roughnessvalue)

        # Add to dictionary
        self.crosssection_def[name] = {
            'id' : name,
            'type': 'circle',
            'diameter': diameter,
            'roughnessnames': roughnessname
        }

        return name

    def add_rectangle_definition(self, height, width, closed, roughnesstype, roughnessvalue, name=None):
        """
        Add rectangle cross section. The cross section name is derived from the shape and roughness,
        so similar cross sections will result in a single definition.
        """        
        # Get name if not given
        if name is None:
            name = f'rect_h{height:.3f}_w{width:.3f}'

        # Get roughnessname
        roughnessname = self.get_roughnessname(roughnesstype, roughnessvalue)

        # Add to dictionary
        self.crosssection_def[name] = {
            'id' : name,
            'type': 'rectangle',
            'height': height,
            'width': width,
            'closed': int(closed),
            'roughnessnames': roughnessname
        }

        return name

    def add_trapezium_definition(self, slope, maximumflowwidth, bottomwidth, closed, roughnesstype, roughnessvalue, name=None):
        """
        Add rectangle cross section. The cross section name is derived from the shape and roughness,
        so similar cross sections will result in a single definition.
        """        
        # Get name if not given
        if name is None:
            name = f'trapz_s{slope:.1f}_bw{bottomwidth:.1f}_bw{maximumflowwidth:.1f}'
        
        # Get roughnessname
        roughnessname = self.get_roughnessname(roughnesstype, roughnessvalue)

        # Add to dictionary
        self.crosssection_def[name] = {
            'id' : name,
            'type': 'trapezium',
            'slope': slope,
            'maximumflowwidth': maximumflowwidth,
            'bottomwidth': bottomwidth,
            'closed': int(closed),
            'roughnessnames': roughnessname
        }

        return name

    def add_crosssection_location(self, branchid, chainage, definition, minz=np.nan, shift=0.0):

        descr = f'{branchid}_{chainage:.1f}'
        # Add cross section location
        self.crosssection_loc[descr] = {
            'id': descr,
            'branchid': branchid,
            'chainage': chainage,
            'shift': shift,
            'definition': definition,
        }

    def get_branches_without_crosssection(self):
        # First find all branches that match a cross section
        branch_ids = {dct['branchid'] for _, dct in self.crosssection_loc.items()}
        # Select the branch-ids that do nog have a matching cross section
        branches = self.dflowfmmodel.network.branches
        no_crosssection = branches.index[~np.isin(branches.index, list(branch_ids))]

        return no_crosssection.tolist()

    def get_bottom_levels(self):
        """Method to determine bottom levels from cross sections"""

        # Initialize lists
        data = []
        geometry = []
        
        for key, css in self.crosssection_loc.items():
            # Get location
            geometry.append(self.dflowfmmodel.network.schematised.at[css['branchid'], 'geometry'].interpolate(css['chainage']))
            shift = css['shift']

            # Get depth from definition if yz and shift
            definition = self.crosssection_def[css['definition']]
            minz = shift
            if definition['type'] == 'yz':
                minz += min(float(z) for z in definition['zvalues'].split())
            
            data.append([css['branchid'], css['chainage'], minz])

        # Add to geodataframe
        gdf = gpd.GeoDataFrame(
            data=data,
            columns=['branchid', 'chainage', 'minz'],
            geometry=geometry
        )

        return gdf



class Links1d2d:

    def __init__(self, network):
        self.mesh1d = network.mesh1d
        self.mesh2d = network.mesh2d
        self.network = network

        self.nodes1d = []
        self.faces2d = []

    def generate_1d_to_2d(self, max_distance=np.inf):
        """
        Generate 1d2d links from 1d nodes. Each 1d node is connected to
        the nearest 2d cell. A maximum distance can be specified to remove links
        that are too long.
        """
        logger.info(f'Generating links from 1d to 2d based on distance.')
        
        # Create KDTree for faces
        faces2d = np.c_[self.mesh2d.get_values('facex'), self.mesh2d.get_values('facey')]
        get_nearest = KDTree(faces2d)

        # Get network geometry
        all_1d_nodes = self.mesh1d.get_nodes()

        # Get nearest 2d nodes
        distance, idx_nearest = get_nearest.query(all_1d_nodes)
        close = (distance < max_distance)
        
        # Add link data
        self.nodes1d.extend(np.arange(len(all_1d_nodes))[close] + 1)
        self.faces2d.extend(idx_nearest[close] + 1)

        # Remove conflicting 1d2d links
        for bc in self.network.dflowfmmodel.external_forcings.boundaries.itertuples():
            if bc.geometry is None:
                continue
            self.check_boundary_link(bc)

    def generate_2d_to_1d(self, max_distance=np.inf, intersecting=True):
        """
        Generate 1d2d links from 2d cells, meaning that for the option:
        1. intersecting = True: each 2d cell crossing a 1d branch is connected to
            the nearest 1d cell.
        2. intersecting = False: each 2d cell is connected to the nearest 1d cell,
            if the link does not cross another cell.
        A maximum distance can be specified to remove links that are too long. In
        case of option 2. setting a max distance will speed up the the process a bit.
        """
        logger.info(f'Generating links from 2d to 1d based on {"intersection" if intersecting else "distance"}.')

        # Collect polygons for cells
        centers2d = self.mesh2d.get_faces(geometry='center')
        idx = np.arange(len(centers2d), dtype='int')
        # Create KDTree for 1d cells
        nodes1d = self.mesh1d.get_nodes()
        get_nearest = KDTree(nodes1d)

        
        # Make a pre-selection
        if max_distance < np.inf:
            # Determine distance from 2d to nearest 1d
            distance, _ = get_nearest.query(centers2d)
            idx = idx[distance < max_distance]
        
        # Create GeoDataFrame
        logger.info(f'Creating GeoDataFrame of ({len(idx)}) 2D cells.')
        cells = gpd.GeoDataFrame(
            data=centers2d[idx],
            columns=['x', 'y'],
            index=idx + 1,
            geometry=[Polygon(cell) for i, cell in enumerate(self.mesh2d.get_faces()) if i in idx]
        )
        
        # Find intersecting cells with branches
        logger.info('Determine intersecting or nearest branches.')
        branches = self.network.branches
        if intersecting:
            geometry.find_nearest_branch(branches, cells, method='intersecting')
        else:
            geometry.find_nearest_branch(branches, cells, method='overal', maxdist=max_distance)

        # Drop the cells without intersection
        cells.dropna(subset=['branch_offset'], inplace=True)
        faces2d = np.c_[cells.x, cells.y]
        
        # Get nearest 1d nodes
        distance, idx_nearest = get_nearest.query(faces2d)
        close = (distance < max_distance)
        
        # Add link data
        self.nodes1d.extend(idx_nearest[close] + 1)
        self.faces2d.extend(cells.index.values[close])

        if not intersecting:
            logger.info('Remove links that cross another 2D cell.')
            # Make sure only the nearest cells are accounted by removing all links that also cross another cell
            links = self.get_1d2dlinks(as_gdf=True)
            todrop = []

            # Remove links that intersect multiple cells
            cellbounds = cells.bounds.values.T
            for link in tqdm.tqdm(links.itertuples(), total=len(links), desc='Removing links crossing mult. cells'):
                selectie = cells.loc[possibly_intersecting(cellbounds, link.geometry)].copy()
                if selectie.intersects(link.geometry).sum() > 1:
                    todrop.append(link.Index)
            links.drop(todrop, inplace=True)

            # Re-assign
            del self.nodes1d[:]
            del self.faces2d[:]

            self.nodes1d.extend(links['node1did'].values.tolist())
            self.faces2d.extend(links['face2did'].values.tolist())

        # Remove conflicting 1d2d links
        for bc in self.network.dflowfmmodel.external_forcings.boundaries.itertuples():
            if bc.geometry is None:
                continue
            self.check_boundary_link(bc)

    def check_boundary_link(self, bc):
        """
        Since a boundary conditions is not picked up when there is a bifurcation
        in the first branch segment, potential 1d2d links should be removed.

        This function should be called whenever a boundary conditions is added,
        or the 1d2d links are generated.
        """

        # Can only be done after links have been generated
        if not self.nodes1d or not self.faces2d:
            return None

        # Find the nearest node with the KDTree
        nodes1d = self.mesh1d.get_nodes()
        get_nearest = KDTree(nodes1d)
        distance, idx_nearest = get_nearest.query(bc.geometry.centroid)
        node_id = idx_nearest + 1

        # Check 1. Determine if the nearest node itself is not a bifurcation
        edge_nodes = self.mesh1d.get_values('edge_nodes', as_array=True)
        counts = {u: c for u, c in zip(*np.unique(edge_nodes, return_counts=True))}
        if counts[node_id] > 1:
            logger.warning(f'The boundary condition at {nodes1d[idx_nearest]} is not a branch end. Check if it is picked up by dflowfm.')

        # Check 2. Check if any 1d2d links are connected to the node or next node. If so, remove.
        # Find the node(s) connect to 'node_id'
        to_remove = np.unique(edge_nodes[(edge_nodes == node_id).any(axis=1)])
        for item in to_remove:
            while item in self.nodes1d:
                loc = self.nodes1d.index(item)
                self.nodes1d.pop(loc)
                self.faces2d.pop(loc)
                nx, ny = nodes1d[item-1]
                bcx, bcy = bc.geometry.centroid.coords[0]
                logger.info(f'Removed link(s) from 1d node: ({nx:.2f}, {ny:.2f}) because it is too close to boundary condition at ({bcx:.2f}, {bcy:.2f}).')
            
    def get_1d2dlinks(self, as_gdf=False):
        """
        Method to get 1d2d links as array with coordinates or geodataframe.

        Parameters
        ----------
        as_gdf : bool
            Whether to export as geodataframe (True) or numpy array (False)
        """

        if not any(self.nodes1d):
            return None

        # Get 1d nodes and 2d faces
        nodes1d = self.mesh1d.get_nodes()
        faces2d = self.mesh2d.get_faces(geometry='center')

        # Get links
        links = np.dstack([nodes1d[np.array(self.nodes1d) - 1], faces2d[np.array(self.faces2d) - 1]])

        if not as_gdf:
            return np.array([line.T for line in links])
        else:
            return gpd.GeoDataFrame(
                data=np.c_[self.nodes1d, self.faces2d],
                columns=['node1did', 'face2did'],
                geometry=[LineString(line.T) for line in links]
            )

    def remove_1d2d_from_numlimdt(self, file, threshold, node='2d'):
        """
        Remove 1d2d links based on numlimdt file
        """
        if node == '1d':
            links = self.get_1d2dlinks(as_gdf=True)

        with open(file) as f:
            for line in f.readlines():
                x, y, n = line.split()
                if int(n) >= threshold:
                    if node == '2d':
                        self.remove_1d2d_link(float(x), float(y), mesh=node, max_distance=2.0)
                    else:
                        # Find the 1d node connected to the link
                        idx = links.distance(Point(float(x), float(y))).idxmin()
                        x, y = links.at[idx, 'geometry'].coords[0]
                        self.remove_1d2d_link(x, y, mesh=node, max_distance=2.0)

    def remove_1d2d_link(self, x, y, mesh, max_distance):
        """
        Remove 1d 2d link based on x y coordinate.
        Mesh can specified, 1d or 2d.
        """
        if mesh == '1d':
            pts = self.mesh1d.get_nodes()
            ilink = 0
        elif mesh == '2d':
            pts = np.c_[self.mesh2d.get_faces(geometry='center')]
            ilink = 1
        else:
            raise ValueError()

        # Find nearest link
        dists = np.hypot(pts[:, 0] - x, pts[:, 1] - y)
        if dists.min() > max_distance:
            return None
        imin = np.argmin(dists)

        # Determine what rows to remove (if any)
        linkdim = self.nodes1d if mesh == '1d' else self.faces2d
        to_remove = [link for link in (linkdim) if link == (imin + 1)]
        for item in to_remove:
            while item in linkdim:
                loc = linkdim.index(item)
                self.nodes1d.pop(loc)
                self.faces2d.pop(loc)

class Network:

    def __init__(self, dflowfmmodel):
        # Link dflowmodel
        self.dflowfmmodel = dflowfmmodel

        # Mesh 1d offsets
        self.offsets = {}

        # Branches and schematised branches
        self.branches = ExtendedGeoDataFrame(geotype=LineString, required_columns=['code', 'geometry', 'ruwheidstypecode', 'ruwheidswaarde'])
        self.schematised = ExtendedGeoDataFrame(geotype=LineString, required_columns=['geometry'])

        # Create mesh for the 1d network
        self.mesh1d = meshgeom(meshgeomdim())
        self.mesh1d.meshgeomdim.dim = 1

        # Create mesh for the 1d network
        self.mesh2d = meshgeom(meshgeomdim())
        self.mesh2d.meshgeomdim.dim = 2

        # Create 1d2dlinks
        self.links1d2d = Links1d2d(self)

        # Dictionary for roughness definitions
        self.roughness_definitions = {}

        # Link mdu parameters
        self.mdu_parameters = dflowfmmodel.mdu_parameters
        
    def set_branches(self, branches):
        """
        Set branches from geodataframe
        """
        # Check input
        checks.check_argument(branches, 'branches', (ExtendedGeoDataFrame, gpd.GeoDataFrame))
        # Add data to branches
        self.branches.set_data(branches[self.branches.required_columns])
        # Copy branches to schematised
        self.schematised.set_data(self.branches[self.schematised.required_columns])
        
    # generate network and 1d mesh
    def generate_1dnetwork(self, one_d_mesh_distance=40.0, seperate_structures=True):
        """
        Parameters
        ----------
        one_d_mesh_distance : float

        single_edge_nodes : boolean
        """

        if self.branches.empty:
            raise ValueError('Branches should be added before 1d network can be generated.')

        checks.check_argument(one_d_mesh_distance, 'one_d_mesh_distance', (float, int))

        # Temporary dictionary to store the id number of the nodes and branches
        node_ids = []
        nodes = []
        edge_nodes_dict = {}

        # Check if any structures present (if not, structures will be None)
        structures = self.dflowfmmodel.structures.as_dataframe(weirs=True, culverts=True, pumps=True)

        # If offsets are not predefined, generate them base on one_d_mesh_distance
        if not self.offsets:
            self.generate_offsets(one_d_mesh_distance, structures=structures)

        # Add the network data to the 1d mesh structure
        sorted_branches = self.branches.iloc[self.branches.length.argsort().values]

        # Add network branch data
        dimensions = self.mesh1d.meshgeomdim
        dimensions.nbranches = len(sorted_branches)
        self.mesh1d.set_values('nbranchorder', (np.ones(dimensions.nbranches, dtype=int) * -1).tolist())
        self.mesh1d.set_values('nbranchlengths', sorted_branches.geometry.length + 1e-12)
        self.mesh1d.description1d['network_branch_ids'] = sorted_branches.index.tolist()
        self.mesh1d.description1d['network_branch_long_names'] = sorted_branches.index.tolist()
        
        # Add network branch geometry
        coords = [line.coords[:] for line in sorted_branches.geometry]
        geomx, geomy = list(zip(*list(itertools.chain(*coords))))
        dimensions.ngeometry = len(geomx)
        self.mesh1d.set_values('nbranchgeometrynodes', [len(lst) for lst in coords])
        self.mesh1d.set_values('ngeopointx', geomx)
        self.mesh1d.set_values('ngeopointy', geomy)

        branch_names = sorted_branches.index.tolist()
        branch_longnames = 'long_' + sorted_branches.index

        network_edge_nodes = []
        mesh1d_edge_nodes = []
        mesh1d_branchidx = []
        mesh1d_branchoffset = []
        mesh1d_node_names = []

        # For each branch
        for i_branch, branch in enumerate(sorted_branches.itertuples()):

            # Get branch coordinates
            points = branch.geometry.coords[:]

            # Network edge node administration
            # -------------------------------
            first_point = points[0]
            last_point = points[-1]
            
            # Get offsets from dictionary
            offsets = self.offsets[branch.Index]
            # The number of links on the branch
            nlinks = len(offsets) - 1
            
            # Check if the first and last point of the branch are already in the set
            if (first_point not in nodes):
                first_present = False
                nodes.append(first_point)
            else:
                first_present = True
                offsets = offsets[1:]
                
            if (last_point not in nodes):
                last_present = False
                nodes.append(last_point)
            else:
                last_present = True
                offsets = offsets[:-1]
            
            # If no points remain, add an extra halfway: each branch should have at least 1 node
            if len(offsets) == 0:
                offsets = np.array([branch.geometry.length / 2.])
                nlinks += 1
                
            # Get the index of the first and last node in the dictionary (1 based, so +1)
            i_from = nodes.index(first_point) + 1
            i_to = nodes.index(last_point) + 1
            if i_from == i_to:
                raise ValueError('Start and end node are the same. Ring geometries are not accepted.')
            network_edge_nodes.append([i_from, i_to])
            
            # Mesh1d edge node administration
            # -------------------------------
            # First determine the start index. This is equal to the number of already present points (+1, since 1 based)
            start_index = len(mesh1d_branchidx) + 1
            # For each link, create a new edge node connection
            if first_present:
                start_index -= 1
            new_edge_nodes = [[start_index + i, start_index + i + 1] for i in range(nlinks)]
            # If the first node is present, change the first point of the first edge to the existing point
            if first_present:
                new_edge_nodes[0][0] = edge_nodes_dict[first_point]
            else:
                edge_nodes_dict[first_point] = new_edge_nodes[0][0]
            # If the last node is present, change the last point of the last edge too
            if last_present:
                new_edge_nodes[-1][1] = edge_nodes_dict[last_point]
            else:
                edge_nodes_dict[last_point] = new_edge_nodes[-1][1]
            # Add to edge_nodes
            mesh1d_edge_nodes.extend(new_edge_nodes)
            
            # Update number of nodes
            mesh_point_names = [f'{branch.Index}_{offset:.2f}' for offset in offsets]
            
            # Append ids, longnames, branch and offset
            self.mesh1d.description1d['mesh1d_node_ids'].extend(mesh_point_names)
            self.mesh1d.description1d['mesh1d_node_long_names'].extend(mesh_point_names)
            mesh1d_branchidx.extend([i_branch + 1] * len(offsets))
            mesh1d_branchoffset.extend(offsets.tolist())
            
        # Parse nodes
        dimensions.nnodes = len(nodes)
        nodex, nodey = list(zip(*nodes))
        self.mesh1d.set_values('nnodex', nodex)
        self.mesh1d.set_values('nnodey', nodey)
        self.mesh1d.description1d['network_node_ids'].extend([f'{x:.0f}_{y:.0f}' for x, y in nodes])
        self.mesh1d.description1d["network_node_long_names"].extend([f'x={x:.0f}_y={y:.0f}' for x, y in nodes])

        # Add edge node data to mesh
        self.mesh1d.set_values('nedge_nodes', np.ravel(network_edge_nodes))
        self.mesh1d.meshgeomdim.numedge = len(mesh1d_edge_nodes)
        self.mesh1d.set_values('edge_nodes', np.ravel(mesh1d_edge_nodes))

        # Add mesh branchidx and offset to mesh
        dimensions.numnode = len(mesh1d_branchidx)
        self.mesh1d.set_values('branchidx', mesh1d_branchidx)
        self.mesh1d.set_values('branchoffsets', mesh1d_branchoffset)

        # Process the 1d network (determine x and y locations) and determine schematised branches
        schematised, _ = self.mesh1d.process_1d_network()
        for idx, geometry in schematised.items():
            self.schematised.at[idx, 'geometry'] = geometry
            
    def _generate_1d_spacing(self, anchor_pts, one_d_mesh_distance):
        """
        Generates 1d distances, called by function generate offsets
        """
        offsets = []
        for i in range(len(anchor_pts) - 1):
            section_length = anchor_pts[i+1] - anchor_pts[i]
            if section_length <= 0.0:
                raise ValueError('Section length must be larger than 0.0')
            nnodes = max(2, int(round(section_length / one_d_mesh_distance) + 1))
            offsets.extend(np.linspace(anchor_pts[i], anchor_pts[i+1], nnodes - 1, endpoint=False).tolist())
        offsets.append(anchor_pts[-1])

        return np.asarray(offsets)

    def generate_offsets(self, one_d_mesh_distance, structures=None):
        """
        Method to generate 1d network grid point locations. The distances are generated
        based on the 1d mesh distance and anchor points. The anchor points can for
        example be structures; every structure should be seperated by a gridpoint.
        """
        # For each branch
        for branch in self.branches.itertuples():
            # Distribute points along network [1d mesh]
            offsets = self._generate_1d_spacing([0.0, branch.geometry.length], one_d_mesh_distance)
            self.offsets[branch.Index] = offsets
        
        if structures is not None:
            # Check argument
            checks.check_argument(structures, 'structures', (pd.DataFrame, gpd.GeoDataFrame), columns=['branchid', 'chainage'])

            # Get structure data from dfs
            ids_offsets = structures[['branchid', 'chainage']]
            idx = (structures['branchid'] == '')
            if idx.any():
                logger.warning('Some structures are not linked to a branch.')
            ids_offsets = ids_offsets.loc[idx, :]

            # For each branch
            for branch_id, group in ids_offsets.groupby('branchid'):

                # Check if structures are located at the same offset
                u, c = np.unique(group['chainage'], return_counts=True)
                if any(c > 1):
                    logger.warning('Structures {} have the same location.'.format(
                        ', '.join(group.loc[np.isin(group['chainage'], u[c>1])].index.tolist())))
                
                branch = self.branches.at[branch_id, 'geometry']
                limits = sorted(group['chainage'].unique())
                
                anchor_pts = [0.0, branch.length]
                offsets = self._generate_1d_spacing(anchor_pts, one_d_mesh_distance)

                if any(limits):
                    upper_limits = limits + [branch.length + 0.1]
                    lower_limits = [-0.1] + limits
                    
                    # Determine the segments that are missing a grid point
                    in_range = [((offsets > lower) & (offsets < upper)).any() for lower, upper in zip(lower_limits, upper_limits)]

                    while not all(in_range):
                        # Get the index of the first segment without grid point
                        i = in_range.index(False)
                        
                        # Add it to the anchor pts
                        anchor_pts.append((lower_limits[i] + upper_limits[i]) / 2.)
                        anchor_pts = sorted(anchor_pts)
                        
                        # Generate new offsets
                        offsets = self._generate_1d_spacing(anchor_pts, one_d_mesh_distance)
                
                        # Determine the segments that are missing a grid point
                        in_range = [((offsets > lower) & (offsets < upper)).any() for lower, upper in zip(lower_limits, upper_limits)]
                    
                    if len(anchor_pts) > 2:
                        logger.info(f'Added 1d mesh nodes on branch {branch_id} at: {anchor_pts}, due to the structures at {limits}.')

                # Set offsets for branch id
                self.offsets[branch_id] = offsets

    def get_roughness_description(self, roughnesstype, value):

        # Check input
        checks.check_argument(roughnesstype, 'roughness type', (str, int))
        checks.check_argument(value, 'roughness value', (float, int, np.float, np.integer))

        # Convert integer to string
        if isinstance(roughnesstype, int):
            roughnesstype = roughness_gml[roughnesstype]
    	
        # Get name
        name = f'{roughnesstype}_{value}'

        # Check if the description is already known
        if name.lower() in map(str.lower, self.roughness_definitions.keys()):
            return name

        # Convert roughness type string to integer for dflowfm
        delft3dfmtype = roughness_delft3dfm[roughnesstype.lower()]

        # Add to dict
        self.roughness_definitions[name] = {
            'name': name,
            'code': delft3dfmtype,
            'value': value
        }

        return name

    def add_mesh2d(self, twodmesh):
        """
        Add 2d mesh to network object.
        """
        if not hasattr(twodmesh, 'meshgeom'):
            checks.check_argument(twodmesh, 'twodmesh', meshgeom)
            geometries = twodmesh
        else:
            if not isinstance(twodmesh.meshgeom, meshgeom):
                raise TypeError('The given mesh should have an attribute "meshgeom".')
            geometries = twodmesh.meshgeom

        # Add the meshgeom
        self.mesh2d.add_from_other(geometries)

        # Add mdu parameters
        if hasattr(twodmesh, 'missing_z_value'):
            if twodmesh.missing_z_value is not None:
                self.mdu_parameters['BedlevUni'] = twodmesh.missing_z_value

    def get_node_idx_offset(self, branch_id, pt, nnodes=1):
        """
        Get the index and offset of a node on a 1d branch.
        The nearest node is looked for.
        """

        # Project the point on the branch
        dist = self.schematised[branch_id].project(pt)

        # Get the branch data from the networkdata
        branchidx = self.mesh1d.description1d['network_branch_ids'].index(self.str2chars(branch_id, self.idstrlength)) + 1
        pt_branch_id = self.mesh1d.get_values('branchidx', as_array=True)
        idx = np.where(pt_branch_id == branchidx)
        
        # Find nearest offset
        offsets = self.mesh1d.get_values('branchoffset', as_array=True)[idx]
        isorted = np.argsort(np.absolute(offsets - dist))
        isorted = isorted[:min(nnodes, len(isorted))]

        # Get the offset
        offset = [offsets[imin] for imin in isorted]
        # Get the id of the node
        node_id = [idx[0][imin] + 1 for imin in isorted]

        return node_id, offset


class Structures:

    def __init__(self, dflowfmmodel):
        self.pumps = {}
        self.weirs = {}
        self.culverts = {}
        
        self.dflowfmmodel = dflowfmmodel

        # Create the io class
        self.io = dfmreader.StructuresIO(self)

    def add_pump(self, id, branchid, chainage, direction, nrstages, capacity, startlevelsuctionside, stoplevelsuctionside, locationfile):
        self.pumps[id] = {
            "type": "pump",
            'id': id,
            'branchid': branchid,
            'chainage': chainage,
            'direction': direction,
            'nrstages': nrstages,
            'capacity': capacity,
            'startlevelsuctionside': startlevelsuctionside,
            'stoplevelsuctionside': stoplevelsuctionside,
            'locationfile': f'pump_{id}.pli'
        }

    def add_weir(self, id, branchid, chainage, crestlevel, crestwidth, dischargecoeff=1.0, latdiscoeff=1.0, allowedflowdir=0):
        self.weirs[id] = {
            "type": "weir",
            'id': id,
            'branchid': branchid,
            'chainage': chainage,
            'crestlevel': crestlevel,
            'crestwidth': crestwidth,
            'dischargecoeff': dischargecoeff,
            'latdiscoeff': latdiscoeff,
            'allowedflowdir': allowedflowdir
        }

    def add_culvert(self, id, branchid, chainage, leftlevel, rightlevel, crosssection, length, inletlosscoeff,
                    outletlosscoeff, allowedflowdir=0, valveonoff=0, inivalveopen=0.0, losscoeffcount=0,
                    frictiontype='StricklerKs', frictionvalue=75.0):
        """
        Add a culvert to the schematisation.

        Note that the cross section should be handed as dictionary. This should contain the
        shape (circle, rectangle) and the required arguments.
        
        """

        # Check the content of the cross section dictionary
        checks.check_dictionary(crosssection, required='shape', choice=['diameter', ['width', 'height', 'closed']])

        # Get roughnessname
        roughnessname = self.dflowfmmodel.network.get_roughness_description(frictiontype, frictionvalue)

        # Add cross section definition
        if crosssection['shape'] == 'circle':
            definition = self.dflowfmmodel.crosssections.add_circle_definition(crosssection['diameter'], frictiontype, frictionvalue)
        elif crosssection['shape'] == 'rectangle':
            definition = self.dflowfmmodel.crosssections.add_rectangle_definition(
                crosssection['height'], crosssection['width'], crosssection['closed'], frictiontype, frictionvalue)
        else:
            NotImplementedError(f'Cross section with shape \"{crosssection["shape"]}\" not implemented.')

        # Add the culvert to the dictionary
        self.culverts[id] = {
            "type": "culvert",
            "id": id,
            "branchid": branchid,
            "chainage": chainage,
            "allowedflowdir": allowedflowdir,
            "leftlevel": leftlevel,
            "rightlevel": rightlevel,
            "csdefid": definition,
            "length": round(length, 3),
            "inletlosscoeff": inletlosscoeff,
            "outletlosscoeff": outletlosscoeff,
            "valveonoff": valveonoff,
            "inivalveopen": inivalveopen,
            "losscoeffcount": losscoeffcount,
            "bedfrictiontype": roughness_delft3dfm[frictiontype.lower()],
            "bedfriction": frictionvalue,
            "groundfrictiontype": roughness_delft3dfm[frictiontype.lower()],
            "groundfriction": frictionvalue
        }

    def as_dataframe(self, pumps=False, weirs=False, culverts=False):
        """
        Returns a dataframe with the structures. Specify with the keyword arguments what structure types need to be returned.
        """
        dfs = []
        for df, descr, add in zip([self.culverts, self.weirs, self.pumps], ['culvert', 'weir', 'pump'], [culverts, weirs, pumps]):
            if any(df) and add:
                df = pd.DataFrame.from_dict(df, orient='index')
                df.insert(loc=0, column='structype', value=descr, allow_duplicates=True)
                dfs.append(df)

        if len(dfs) > 0:
            return pd.concat(dfs, sort=False)
        
class ObservationPoints(ExtendedGeoDataFrame):

    def __init__(self, dflowfmmodel):
        super(ObservationPoints, self).__init__(geotype=Point, required_columns=['name', 'branchid', 'offset', 'geometry'])

        self._metadata.append('dflowfmmodel')
        self.dflowfmmodel = dflowfmmodel

    def add_point(self, crd, name, snap_to_1d=True):
        """
        Method to add a single observation points. Uses
        the method add_points to add the point.
        """
        self.add_points([crd], [name], snap_to_1d)

    def add_points(self, crds, names, snap_to_1d=True):
        """
        Method to add observation points to schematisation. An option
        'snap' is available, that snaps the points to the nearest branch,
        and after that to the nearest calculation point (offset). This
        prevents delft3dfm to find a nearer calculation location to read
        the data from.

        Parameters
        ----------
        crds : Nx2 list or array
            x and y coordinates of observation points
        names : list
            names of the observation points
        snap_to_1d : bool
            whether to snap the 1d points to the 1d network
            and calculation points. Default is True.
        """

        # Check if data for snapping is available
        network = self.dflowfmmodel.network
        if snap_to_1d and not network.mesh1d.meshgeomdim.numnode:
            raise ValueError('The network geometry should be generaterd before the observation points can be snapped to 1d.')

        branches = [None] * len(crds)
        offsets = [None] * len(crds)
        if snap_to_1d:
            snapped_pts = []
            for i, xy in enumerate(crds):
                # Find nearest branch
                pt = Point(*xy)
                dist = network.branches.distance(pt)
                branchid = dist.idxmin()
                
                # Find nearest offset
                branch = network.branches.at[branchid, 'geometry']
                projected_dist = branch.project(pt)
                offsets[i] = network.offsets[branchid][np.argmin(np.absolute(network.offsets[branchid] - projected_dist))]
                
                # Determine location of grid point
                snapped_pts.append(branch.interpolate(offsets[i]))
                branches[i] = branchid
        else:
            snapped_pts = [Point(*crd) for crd in crds]
    
        # Add to dataframe
        for args in zip(names, branches, offsets, snapped_pts):
            self.loc[args[0], :] = args

def list_to_str(lst):
    string = ' '.join([f'{number:6.3f}' for number in lst])
    return string
