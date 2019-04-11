import pandas as pd
import numpy as np
import re
import logging
import boto3
import botocore
import shutil
import pathlib

# TODO - should file_system_provider_root into the provider definition I think
file_system_provider_root = '../examples/'
temp_folder = './temp/'

logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# TODO - think BUCKET NAME is no longer required (part of object name) but if not, should be moved to the S3 provider definition
BUCKET_NAME = 'daqual'  # replace with your bucket name

# TODO - s3 resource should be defined in the provider definition, rather than polute the global namespace
s3 = boto3.resource('s3')

class Daqual:

    # make sure the default scoring function are easily visible outside the class
    global score_row_count
    global score_column_names
    global score_no_blanks
    global score_column_valid_values
    global score_every_master_value_used
    global score_unique_column
    global score_number
    global score_float
    global score_int
    global score_match
    global score_column_count


    # when creating a Daqual object we supply a "provider definition" to map provider-specific functions to generic
    # capabilities.  Example providers/provider-mappings are implemented at the end of the class definition to make
    # use of specific provider-functionality implemented as part of the base Daqal implementation (for S3 and local
    # filesystem providers)
    def __init__(self, provider):
        self.object_list = {}
        self.provider=provider


    # retrieve objects from the filesystem and return a pandas DataFrame
    # intended primarily for easy/local development
    def retrieve_object_from_filesystem(objectkey):
        bucket, file_objectkey = objectkey.split('/',1)
        pathlib.Path(temp_folder+bucket).mkdir(parents=True, exist_ok=True)
        tempfilename=temp_folder + file_objectkey

        filename = file_system_provider_root + objectkey
        shutil.copyfile(filename,tempfilename)
        df = pd.read_csv(tempfilename)
        # TODO - tidy up/remove tempfile when the instance is destroyed

        logger.info("Retrieved and converted object {}".format(filename))
        return (1,df)


    # retrieve objects from S3 and return a pandas DataFrame
    def retrieve_object_from_S3(objectkey):
        # TODO - switch from using /temp to perhaps /temp and then a unique-per-instance identifier
        bucket, s3_objectkey = objectkey.split('/',1)
        pathlib.Path(temp_folder+bucket).mkdir(parents=True, exist_ok=True)
        tempfilename=temp_folder + objectkey
        try:
            s3.Bucket(bucket).download_file(s3_objectkey, tempfilename)
        except botocore.exceptions.ClientError as e:
            logger.error("Could not retrieve object {}".format(objectkey))
            return(0, None)
        df = pd.read_csv(tempfilename)
        # TODO - tidy up/remove tempfile when the instance is destroyed

        logger.info("Retrieved and converted object {}".format(objectkey))
        return (1,df)


    # set object metadata in S3
    def update_object_tagging_S3(self,objectkey, tag, value):
        bucket, s3_objectkey = objectkey.split('/', 1)
        tags = boto3.client('s3').get_object_tagging(Bucket=bucket,Key=s3_objectkey)
        t = tags['TagSet']
        if len(t) >0:
            for i in t:
                if i['Key']==tag:
                    i['Value']=str(value)
        else:
            t.append({'Key': tag, 'Value': str(value)})

        ts = {'TagSet':t}
        boto3.client('s3').put_object_tagging(Bucket=bucket, Key=s3_objectkey, Tagging=ts)

    # set a quality score against the object
    def set_quality_score(self,objectkey, quality):
        logger.info("Setting quality_score on {} to {}".format(objectkey,quality))
        self.provider['tag'](objectkey,'quality_score', quality)

    # simply retrieve a particular dataframe
    def get_dataframe(self,object_name):
        return self.object_list[object_name]['dataframe']

    # TODO - retrieve a file (or a filename) (rather than an object/dataframe)
    # this may be useful for use-cases where we want to play withe the raw files
    def get_file(self,object_name):
        return None


    # The primary function of Daqual - to iterate over a list of tests, to run a test against an object and to record
    # a measure of the quality of that object, and to form a measure of the overall quality of the set of tests
    # defined by that list.
    #
    # Each item in the validation list is defined thus:
    # [object_name, scoring_function, function parameter object/dict, weight, threshold]
    #
    # The scoring function will return a value between 0 and 1; the weighting is also a value between 0 and 1 and is a
    # facility to allow different tests to contribute differently to the overall quality assessment for sophisticated
    # test models.  The threshold is also a value between 0 and 1, and defines the minimum quality expected for that
    # particular test for the entire "test set" to pass (or fail). A threshold of 1 requires a quality score for that
    # test to be 1 for the "test set" to pass.
    def validate_objects(self,validation_list):

        # first retrieve all required objects, create dataframes for them
        for item in validation_list:
            object_key = item[0]
            if object_key not in self.object_list.keys():    # if we haven't already retrieved and processed this object
                score, df = self.provider['retrieve'](object_key)
                setattr(df,'objectname',object_key) # a convenience such that we can always go in the reverse direction
                                                    # and retrieve the object key from the dataframe
                if score == 1:
                    self.object_list[object_key]={} # create the key and the dict
                    self.object_list[object_key]['dataframe']=df
                    self.object_list[object_key]['quality'] = 0
                    self.object_list[object_key]['n_tests'] = 0
                    self.object_list[object_key]['total_weighting'] = 0
                if score == 0:          # each array is defined such that ALL files in a specific validation list
                    return 0            # must exist

        # if we have all required files, then for each entry in the validation list, we perform the requisite
        # scoring and test, and for each individual object we keep track of the total number of tests, the total
        # weighting, and the cumulative weighted quality score

        failed_an_individual_test=False
        for item in validation_list:
            object_key = item[0]
            scoring_function=item[1]
            function_parameters=item[2]
            individual_weight = item[3]
            individual_threshold = item[4]

            self.object_list[object_key]['n_tests'] += 1
            self.object_list[object_key]['total_weighting'] += individual_weight

            individual_test_score = item[1](self,object_key, item[2])
            logger.info('Validating {} with test {}({}) - Quality Score = {}'.format(object_key,
                                                                                     scoring_function.__name__,
                                                                                     function_parameters,
                                                                                     individual_test_score))
            # if an individual test fails its minimum threshold then we need to record that fact and "fail" the overall
            # validation/quality assessment
            if (individual_test_score < individual_threshold):
                logger.warn("Threshold failure: {} {}({}) scored {}, expecting at least {}".format(
                    object_key,scoring_function.__name__,function_parameters,
                    individual_test_score,individual_threshold))
                failed_an_individual_test=True

            # we record the contribution to the object quality, even if the test failed a threshold test
            self.object_list[object_key]['quality'] += (individual_weight * individual_test_score)

        # now having completed every test we go through the entire list of results and re-weight the quality score
        # for each object, (which in turn is set or tagged on the object itself for most providers).  We also
        # calculate an "overall quality measure" as the simple average of quality scores for each object in the list
        average_quality = 0
        for i in self.object_list.keys():
            self.object_list[i]['quality'] /= self.object_list[i]['total_weighting']
            self.set_quality_score(i, self.object_list[i]['quality'])
            average_quality += self.object_list[i]['quality']
            logger.info("Object summary for {} - quality: {}, n_tests: {}".format(i, self.object_list[i]['quality'], self.object_list[i]['n_tests']))
        average_quality /= len(self.object_list)

        # TODO - need to actually fail the validation if a threshold is failed
        # probably just uncomment these lines and return the "overall" quality test/measure as being zero
        # if failed_an_individual_test==True:
        #    average_quality=0

        return average_quality, self.object_list # return also the actual list of dataframes, files, etc. Necessary?


    # get the % of columns expected; if you get too many columns, it still returns a % showing your overage, upto a maximum
    # if you have double or more the number of colums desired, the returned score is 0
    #
    # param: expected_n - the number of expected columns
    def score_column_count(self, object_name, p):
        dataframe = self.get_dataframe(object_name)
        score=len(dataframe.columns)/p['expected_n']
        if score <= 1:
            return score
        if (score > 2):
            score = 0
        elif (score > 1):
            score = 2 - score

        logger.warn("Object {} has {} columns, expecting only {}".format(dataframe.objectname,len(dataframe.columns),p['expected_n']))
        return score


    # is a mandatory column complete?
    #
    # param: column name to check
    def score_no_blanks(self,object_name, p):
        df=self.get_dataframe(object_name)
        if p['column'] in df.columns[df.isna().any()].tolist():
            return 0
        else:
            return 1


    # are the column names as expected?
    #
    # param: columns - a list of expected columns
    def score_column_names(self,object_name, p):
        df=self.get_dataframe(object_name)
        if (df.columns.to_list() == p['columns']):
            return 1
        else:
            return 0


    # provide a master data frame, and column therein, and confirm the % of values from our regular dataframe, and column,
    # that are present in that master set. useful check of referential integrity and consistency across files
    #
    # params: column, master, master_column
    def score_column_valid_values(self,object_name,p):
        df=self.get_dataframe(object_name)

        master = self.get_dataframe(p['master'])
        s=df[p['column']].to_list()

        m=master[p['master_column']].to_list()
        c=0
        for i in s:
            if i in m:
                c+=1
        if c < len(s):
            logger.warn('Unexpected values in {} column {}; values are not in master data {} col'.format(
                df['objectname'],p['column'],p['master'],p['master_column']
            ))
        return c/len(s)


    # provide a master data frame, and column therein, and confirm that each and every value from that master list is used
    # at least once in our primary dataframe. returns the percentage of the master list that is indeed used in the primary dataframe
    #
    # params: column, master, master_column
    def score_every_master_value_used(self,object_name,p):
        df=self.get_dataframe(object_name)
        master = self.get_dataframe(p['master'])
        s=df[p['column']].to_list()

        m=set(master[p['master_column']].to_list())
        original_count=len(m)
        c=0
        for i in m:
            if i in s:
                m.remove(i)

        score = (original_count-len(m))/original_count

        return score


    # does a column contain only unique values
    #
    # param: column - the column to check
    def score_unique_column(self,object_name, p):
        df=self.get_dataframe(object_name)
        values = df[p['column']].to_list()
        values_set = set(values)
        if len(values) == len(values_set):
            return 1
        else:
            return 0


    # get the % of rows expected; if you get too many columns, it still returns a % showing your overage, upto a maximum
    # if you have double or more the number of rows desired, the returned score is 0
    #
    # param: expected_row_count - the number of expected rows
    def score_row_count(self,object_name,p):

        df = self.get_dataframe(object_name)
        row_count = len(df.index)
        comparison = p.get('comparison')
        if comparison != None:
            comparison_df = self.get_dataframe(comparison)
            comparison_row_count = len(comparison_df.index)
            expected_row_count = comparison_row_count - p.get('expected_delta')
            score = row_count / expected_row_count
        else:
            score = row_count/p['expected_row_count']

        if score <= 1:
            return score
        if (score > 2):
            score = 0
        elif (score > 1):
            score = 2 - score
        return score


    # score a column for its match against a regular expression, returnint the % of rows that match the regex
    #
    # param: match - the regex to use, column - the column to match
    def score_match(self,object_name, p):
        df = self.get_dataframe(object_name)
        r = p['match']
        pattern = re.compile(r)
        score=0
        for i in df[p['column']]:
            if pattern.match(i):
                score+=1

        score = score/len(df[p['column']])
        return score


    def score_int(self,objectname, p):
        dt = self.get_dataframe(objectname)[p['column']].dtype
        if dt == np.dtype('int64'):
            return 1
        else:
            return 0


    def score_float(self,objectname, p):
        dt = self.get_dataframe(objectname)[p['column']].dtype
        if dt == np.dtype('float64'):
            return 1
        else:
            return 0


    def score_number(self,objectname, p):
        if self.score_float(objectname,p):
            return 1
        else:
            return self.score_int(objectname,p)

    # convenience function to allow "no function" for providers (e.g. see file_system_provider below)
    def nothing(x,y,z):
        return None

    # Define some function mappings for provider-specific behaviour
    file_system_provider = {
        'retrieve': retrieve_object_from_filesystem,
        'tag': nothing    # filesystem provider doesn't currently support tagging
    }

    aws_provider = {
        'retrieve': retrieve_object_from_S3,
        'tag': update_object_tagging_S3
    }
