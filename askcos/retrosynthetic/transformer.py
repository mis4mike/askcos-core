import rdkit.Chem as Chem
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from rdchiral.initialization import rdchiralReaction, rdchiralReactants
from rdchiral.main import rdchiralRun

import askcos.global_config as gc
from askcos.interfaces.template_transformer import TemplateTransformer
from askcos.prioritization.precursors.relevanceheuristic import RelevanceHeuristicPrecursorPrioritizer
from askcos.prioritization.templates.relevance import RelevanceTemplatePrioritizer
from askcos.synthetic.evaluation.fast_filter import FastFilterScorer
from askcos.utilities.banned import BANNED_SMILES
from askcos.utilities.cluster import cluster_precursors
from askcos.utilities.descriptors import rms_molecular_weight, number_of_rings
from askcos.utilities.io.logger import MyLogger

retro_transformer_loc = 'retro_transformer'


class RetroTransformer(TemplateTransformer):
    """Defines an object to perform one-step retrosyntheses for a molecule.

    Attributes:
        mincount (int): Minimum number of precedents for an achiral template for
            inclusion in the template library. Only used when retrotransformers
            need to be initialized.
        mincount_chiral (int): Minimum number of precedents for a chiral
            template for inclusion in the template library. Only used when
            retrotransformers need to be initialized. Chiral templates are
            necessarily more specific, so we generally use a lower threshold
            than achiral templates.
        templates (list): Templates to use for transformation.
        celery (bool): Whether or not Celery is being used.
        TEMPLATE_DB (None or MongoDB): Database to load templates from.
        lookup_only (bool): Whether to only lookup templates in the database
            (instead of loading the entire database).
        precursor_prioritizers (dict): Mapping of precursor prioritizer names to
            objects.
        template_prioritizers (dict):Mapping of template prioritizer names to
            objects.
        precursor_prioritizer (None or str): Specifies which precursor
            prioritizer to use.
        template_prioritizer (None or str): Specifies which template prioritizer
            to use.
        num_templates (int): Number of templates loaded.
        fast_filter (FastFilterScorer or None): Fast filter for evaluation.
        load_all (bool): Whether to load all of the templates into memory.
        use_db (bool): Whether to use the database to look up templates.
    """

    def __init__(
            self, use_db=True, TEMPLATE_DB=None, load_all=gc.PRELOAD_TEMPLATES,
            template_set='reaxys', template_prioritizer='reaxys',
            precursor_prioritizer='relevanceheuristic',
            fast_filter='default', cluster='default',
            cluster_settings=None,
    ):
        """Initializes RetroTransformer.

        Args:
            use_db (bool, optional): Whether to use the database to look up
                templates. (default: {True})
            TEMPLATE_DB (None or MongoDB, optional): Database to load
                templates from. (default: {None})
            load_all (bool, optional): Whether to load all of the templates into
                memory. (default: {gc.PRELOAD_TEMPLATES})
            template_prioritizer (str or Prioritizer): Template prioritizer 
                to use. This can either be 'relevance' or an instance of type 
                Prioritizer the implements a predict method that takes 
                (smiles, max_num_templates, max_cum_prob) arguments and 
                returns np.ndarrays of type np.float32 for (scores, indices)
                of templates to use.
        """

        self.templates = []
        self.template_set = template_set
        self.TEMPLATE_DB = TEMPLATE_DB
        self.template_prioritizer = template_prioritizer
        self.precursor_prioritizer = precursor_prioritizer
        self.fast_filter = fast_filter
        self.cluster = cluster
        self.cluster_settings = cluster_settings or {}

        super(RetroTransformer, self).__init__(load_all=load_all, use_db=use_db)

    def load(self, template_filename=None):
        if template_filename is None:
            template_filename = gc.RETRO_TEMPLATES['file_name']

        if self.template_prioritizer in gc.RELEVANCE_TEMPLATE_PRIORITIZATION:
            MyLogger.print_and_log('Loading template prioritizer for RetroTransformer', retro_transformer_loc)
            template_prioritizer = RelevanceTemplatePrioritizer()
            template_prioritizer.load_model(
                gc.RELEVANCE_TEMPLATE_PRIORITIZATION[self.template_prioritizer]['model_path']
            )
            self.template_prioritizer = template_prioritizer

        if self.precursor_prioritizer == 'relevanceheuristic':
            MyLogger.print_and_log('Loading precursor prioritizer for RetroTransformer', retro_transformer_loc)
            self.precursor_prioritizer_object = RelevanceHeuristicPrecursorPrioritizer()
            self.precursor_prioritizer_object.load_model()
            self.precursor_prioritizer = self.precursor_prioritizer_object.reorder_precursors

        if self.fast_filter == 'default':
            MyLogger.print_and_log('Loading fast filter for RetroTransformer', retro_transformer_loc)
            self.fast_filter_object = FastFilterScorer()
            self.fast_filter_object.load()
            self.fast_filter = lambda x, y: self.fast_filter_object.evaluate(x, y)[0][0]['score']

        if self.cluster == 'default':
            MyLogger.print_and_log('Using default clustering for RetroTransformer', retro_transformer_loc)
            self.cluster = cluster_precursors

        MyLogger.print_and_log('Loading retro-synthetic transformer', retro_transformer_loc)
        if self.use_db:
            self.load_databases()
            try:
                self.TEMPLATE_DB.find_one({})  # check if connection to db exists
                if self.load_all:
                    self.load_from_database()
                    self.use_db = False  # it doesn't make sense to load all templates into memory and then continue to use templates from DB
            except ServerSelectionTimeoutError:
                MyLogger.print_and_log('cannot connect to db, reading from file instead', retro_transformer_loc)
                self.use_db = False
                self.load_from_file(template_filename, self.template_set)
        else:
            MyLogger.print_and_log('reading from file', retro_transformer_loc)
            self.load_from_file(template_filename, self.template_set)

    def get_one_template_by_idx(self, index, template_set=None):
        """Returns one template from given template set with given index.

        Args:
            index (int): index of template to return
            template_set (str): name of template set to return template from

        Returns:
            Template dictionary ready to be applied (i.e. - has 'rxn' object)

        """
        if template_set is None:
            template_set = self.template_set

        if self.use_db:
            db_client = MongoClient(
                gc.MONGO['path'], gc.MONGO['id'], connect=gc.MONGO['connect']
            )
            db_name = gc.RETRO_TEMPLATES['database']
            collection = gc.RETRO_TEMPLATES['collection']
            self.TEMPLATE_DB = db_client[db_name][collection]
            template = self.TEMPLATE_DB.find_one(
                {
                    'index': index,
                    'template_set': template_set
                }
            )
        else:
            template = list(filter(
                lambda x: x['template_set'] == template_set and x['index'] == index,
                self.templates
            ))
            if len(template) != 1:
                raise ValueError('Duplicate templates found when trying to retrieve one unique template!')
            template = template[0]

        if not self.load_all:
            template = self.doc_to_template(template)

        if not template:
            raise ValueError('Could not find template from template set "{}" with index "{}"'.format(
                template_set, index
            ))

        return template

    def order_templates_by_indices(self, indices, template_set=None):
        """Reorders and returns templates given specified indices.

        Handles use of a database, multiple template sets, as well as preloading templates.

        Args:
            indices (np.ndarray): Numpy array of indices to reorder templates.

        Returns:
            Generator of templates to be applied (i.e. - with rxn object)

        """
        if template_set is None:
            template_set = self.template_set

        index_list = indices.tolist()

        if self.use_db:
            templates = list(
                self.TEMPLATE_DB.find(
                    {
                        'index': {'$in': index_list},
                        'template_set': template_set
                    }
                )
            )
        else:
            templates = list(filter(
                lambda x: x['template_set'] == template_set and x['index'] in indices,
                self.templates
            ))

        templates.sort(key=lambda x: index_list.index(x['index']))

        if not self.load_all:
            # return generator of templates with rchiralReaction if rdchiralReaction initialization was successful
            templates = (x for x in (self.doc_to_template(temp) for temp in templates) if x.get('rxn'))

        return templates

    def get_outcomes(
            self, smiles, precursor_prioritizer=None,
            template_set=None, template_prioritizer=None,
            fast_filter=None, fast_filter_threshold=0.75,
            max_num_templates=100, max_cum_prob=0.995,
            cluster_precursors=True, cluster=None, cluster_settings=None,
            selec_check=False, use_ban_list=True, **kwargs
    ):
        """Performs a one-step retrosynthesis given a SMILES string.

        Applies each transformation template sequentially to given target
        molecule to perform retrosynthesis.

        Args:
            smiles (str): Target SMILES string to find precursors for.
            template_prioritizer (optional, Prioritizer): Use to override
                prioritizer created during initialization. This can be 
                any Prioritizer instance that implements a predict method 
                that accepts (smiles, templates, max_num_templates, max_cum_prob) 
                as arguments and returns a (scores, indices) for templates
                up until max_num_templates or max_cum_prob.
            precursor_prioritizer (optional, callable): Use to override
                prioritizer created during initialization. This can be
                any callable function that reorders a list of precursor
                dictionary objects.
            fast_filter (optional, callable): Use to override fast filter
                created during initialization. This can be any callable 
                function that accepts (reactants, products) smiles strings 
                as arguments and returns a score on the range [0.0, 1.0].
            fast_filter_threshold (float): Fast filter threshold to filter
                bad predictions. 1.0 means use all templates.
            cluster_precursors (optional, bool): Whether to run clustering
            cluster (optional, callable): Use to override cluster method.
                This can be any callable that accepts 
                (target, outcomes, **cluster_settings) where target is a smiles 
                string, outcomes is a list of precursor dictionaries, and cluster_settings 
                are cluster specific cluster settings.
            cluster_settings (optional, dict): Dictionary of cluster specific settings
                to be passed to clustering method.
            selec_check (optional, bool): Apply selectivity checking for the predicted precursors
            **kwargs: Additional kwargs to pass through to prioritizers or to
                handle deprecated options.

        Returns:
             RetroResult: Special object for a retrosynthetic expansion result,
                defined by ./results.py
        """

        if template_set is None:
            template_set = self.template_set

        if template_prioritizer is None:
            template_prioritizer = self.template_prioritizer

        if precursor_prioritizer is None:
            precursor_prioritizer = self.precursor_prioritizer

        if fast_filter is None:
            fast_filter = self.fast_filter

        if cluster is None:
            cluster = self.cluster

        if cluster_settings is None:
            cluster_settings = self.cluster_settings

        mol = Chem.MolFromSmiles(smiles)
        smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
        mol = rdchiralReactants(smiles)

        results = []
        smiles_to_index = {}

        if use_ban_list and smiles in BANNED_SMILES:
            return results

        scores, indices = template_prioritizer.predict(
            smiles, max_num_templates=max_num_templates, max_cum_prob=max_cum_prob
        )

        templates = self.order_templates_by_indices(indices, template_set)

        for template, score in zip(templates, scores):
            precursors = self.apply_one_template(mol, template, record_rxn=selec_check)
            for precursor in precursors:
                precursor['template_score'] = score
                joined_smiles = '.'.join(precursor['smiles_split'])
                precursor['rms_molwt'] = -rms_molecular_weight(joined_smiles)
                precursor['num_rings'] = -number_of_rings(joined_smiles)
                precursor['plausibility'] = fast_filter(joined_smiles, smiles)
                # skip if no transformation happened or plausibility is below threshold
                if joined_smiles == smiles or precursor['plausibility'] < fast_filter_threshold:
                    continue
                if joined_smiles in smiles_to_index:
                    res = results[smiles_to_index[joined_smiles]]
                    res['tforms'] |= set([precursor['template_id']])
                    res['num_examples'] += precursor['num_examples']
                    if score > res['template_score']:
                        if selec_check:
                            res['reaction_smarts'] = precursor['reaction_smarts']
                        res['template_score'] = score
                else:
                    precursor['tforms'] = set([precursor['template_id']])
                    smiles_to_index[joined_smiles] = len(results)
                    results.append(precursor)
        for rank, result in enumerate(results, 1):
            result['tforms'] = list(result['tforms'])
            result['rank'] = rank
        results = precursor_prioritizer(results)
        if cluster_precursors:
            cluster_ids = cluster(smiles, results, **cluster_settings)
        for (i, precursor) in enumerate(results):
            if cluster_precursors:
                precursor['group_id'] = cluster_ids[i]
            if selec_check:
                mapped_products, mapped_precursors = self.apply_one_template_to_precursors(precursor['smiles'],
                                                                                           precursor['reaction_smarts'])
                if smiles not in mapped_products:
                    # We couldn't recover the original product for some reason
                    precursor['selec_error'] = True
                    continue
                other_products = [x for x in mapped_products if x != smiles]
                if len(other_products) > 0:
                    precursor['outcomes'] = '.'.join([smiles] + [x for x in other_products])
                    precursor['mapped_outcomes'] = '.'.join([mapped_products[smiles]] + \
                                                            [mapped_products[x] for x in other_products])
                    precursor['mapped_precursors'] = mapped_precursors

        return results

    def apply_one_template(self, mol, template, record_rxn=False):
        """Applies one template to a molecules and returns precursors.

        Args:
            mol (rdchiralReactants): rdchiral reactants molecules to apply
                the template to.
            template (dict): Dictionary representing template to apply. Must 
                have 'rxn' key where value is a rdchiralReaction object.
            record_rxn (bool): wether to include the reaction template smiles in the result

        Returns:
            List of dictionaries representing precursors generated from 
                template application.

        """
        results = []

        try:
            outcomes, mapped_outcomes = rdchiralRun(template['rxn'], mol, return_mapped=True)
        except Exception as e:
            return results

        for j, outcome in enumerate(outcomes):
            smiles_list = []
            smiles_list = outcome.split('.')
            if template['intra_only'] and len(smiles_list) > 1:
                # Disallowed intermolecular reaction
                continue
            if template['dimer_only'] and (len(set(smiles_list)) != 1 or len(smiles_list) != 2):
                # Not a dimer
                continue
            reacting_atoms = mapped_outcomes.get(
                '.'.join(smiles_list), ('.'.join(smiles_list), (-1,))
            )
            result = {
                'smiles': '.'.join(smiles_list),
                'smiles_split': sorted(smiles_list),
                'mapped_smiles': reacting_atoms[0],
                'reacting_atoms': reacting_atoms[1],
                'template_id': str(template['_id']),
                'num_examples': template['count'],
                'necessary_reagent': template['necessary_reagent'],
            }
            if record_rxn:
                result['reaction_smarts'] = template['reaction_smarts']
            results.append(result)

        return results

    def apply_one_template_to_precursors(self, precursors, template):
        """
        Apply one backward template to precursors to get outcomes.

        Args:
            precursors (str): atom mapped smiles for precursors
            template (str): backward template to be applied

        Returns:
            (dict) {smiles: mappedsmiles}
        """
        try:
            products, _, reactants = template.split('>')
            forward_template = '({0})>>({1})'.format(reactants, products)
            forward_rxn = rdchiralReaction(str(forward_template))
            precursor_reacts = rdchiralReactants(precursors)

            outcomes = rdchiralRun(forward_rxn, precursor_reacts, return_mapped=True)
        except Exception as e:
            MyLogger.print_and_log('cannot create forward template from {}'.format(template), retro_transformer_loc)
            return {}, None

        if outcomes:
            _, mapped_products = outcomes
            mapped_products = {k: v[0] for k, v in mapped_products.items()}
        else:
            mapped_products = {}

        mapped_precursors = Chem.MolToSmiles(precursor_reacts.reactants)

        return mapped_products, mapped_precursors

    def apply_one_template_by_idx(
            self, _id, smiles, template_idx, calculate_next_probs=True,
            fast_filter_threshold=0.75, max_num_templates=100, max_cum_prob=0.995,
            template_prioritizer=None, template_set=None, fast_filter=None, use_ban_list=True,
    ):
        """Applies one template by index.

        Args:
            _id (int): Pathway id used by tree builder.
            smiles (str): SMILES string of molecule to apply template to.
            template_idx (int): index of template to apply.
            calculate_next_probs (bool): Fag to caculate probabilies (template 
                relevance scores) for precursors generated by template 
                application.
            fast_filter_threshold (float): Fast filter threshold to filter
                bad predictions. 1.0 means use all templates.
            max_num_templates (int): Maximum number of template scores and 
                indices to return when calculating next probabilities.
            max_cum_prob (float): Maximum cumulative probabilites to use 
                when returning next probabilities.
            template_prioritizer (Prioritizer): Use to override
                prioritizer created during initialization. This can be 
                any Prioritizer instance that implements a predict method 
                that accepts (smiles, templates, max_num_templates, max_cum_prob) 
                as arguments and returns a (scores, indices) for templates
                up until max_num_templates or max_cum_prob.
            template_set (str): Name of template set to use when multiple 
                template sets are available.

        Returns:
            List of outcomes wth (_id, smiles, template_idx, precursors, fast_filter_score)
        """
        if template_prioritizer is None:
            template_prioritizer = self.template_prioritizer

        if template_set is None:
            template_set = self.template_set

        if fast_filter == None:
            fast_filter = self.fast_filter

        mol = Chem.MolFromSmiles(smiles)
        smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
        mol = rdchiralReactants(smiles)

        all_outcomes = []
        seen_reactants = {}
        seen_reactant_combos = []

        if use_ban_list and smiles in BANNED_SMILES:
            all_outcomes.append((_id, smiles, template_idx, [], 0.0))  # dummy outcome
            return all_outcomes

        template = self.get_one_template_by_idx(template_idx, template_set)
        try:
            template['rxn'] = rdchiralReaction(template['reaction_smarts'])
        except ValueError:
            all_outcomes.append((_id, smiles, template_idx, [], 0.0))  # dummy outcome
            return all_outcomes

        for precursor in self.apply_one_template(mol, template):
            reactant_smiles = precursor['smiles']
            if reactant_smiles in seen_reactant_combos:
                continue
            seen_reactant_combos.append(reactant_smiles)
            fast_filter_score = fast_filter(reactant_smiles, smiles)
            if fast_filter_score < fast_filter_threshold:
                continue

            reactants = []
            if calculate_next_probs:
                for reactant_smi in precursor['smiles_split']:
                    if reactant_smi not in seen_reactants:
                        scores, indeces = template_prioritizer.predict(
                            reactant_smi, max_num_templates=max_num_templates, max_cum_prob=max_cum_prob
                        )
                        # scores and indeces will be passed through celery, need to be lists
                        scores = scores.tolist()
                        indeces = indeces.tolist()
                        value = 1
                        seen_reactants[reactant_smi] = (reactant_smi, scores, indeces, value)
                    reactants.append(seen_reactants[reactant_smi])
                all_outcomes.append((_id, smiles, template_idx, reactants, fast_filter_score))
            else:
                all_outcomes.append((_id, smiles, template_idx, precursor['smiles_split'], fast_filter_score))
        if not all_outcomes:
            all_outcomes.append((_id, smiles, template_idx, [], 0.0))  # dummy outcome

        return all_outcomes


if __name__ == '__main__':
    MyLogger.initialize_logFile()
    t = RetroTransformer()
    t.load()  # chiral=True, refs=False, rxns=True)
    # def get_outcomes(
    #             self, smiles, precursor_prioritizer=None,
    #             template_set='reaxys', template_prioritizer=None, 
    #             fast_filter=None, fast_filter_threshold=0.75, 
    #             max_num_templates=100, max_cum_prob=0.995, 
    #             cluster=None, cluster_settings={}, 
    #             **kwargs
    # ):
    # Test using a chiral molecule
    # outcomes = t.get_outcomes('CCOC(=O)[C@H]1C[C@@H](C(=O)N2[C@@H](c3ccccc3)CC[C@@H]2c2ccccc2)[C@@H](c2ccccc2)N1')#, \
    #     #100, (gc.relevanceheuristic, gc.relevance))
    # print(outcomes)

    # #Test using a molecule that give many precursors
    # outcomes = t.get_outcomes('CN(C)CCOC(c1ccccc1)c2ccccc2')#, \
    #     #100, (gc.relevanceheuristic, gc.relevance))
    # print(outcomes)

    # test with one template
    outcomes = t.apply_one_template_by_idx(1,
                                           'CCOC(=O)[C@H]1C[C@@H](C(=O)N2[C@@H](c3ccccc3)CC[C@@H]2c2ccccc2)[C@@H](c2ccccc2)N1',
                                           109659)

    print(outcomes)
